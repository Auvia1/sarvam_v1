import os
import time
import hmac
import hashlib
import json
import uuid
from google import genai
from google.genai import types
from contextlib import asynccontextmanager
from datetime import datetime
import pytz
import redis.asyncio as redis
from dotenv import load_dotenv
from loguru import logger

# ✅ FastAPI and Webhook Imports
import uvicorn
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

try:
    from twilio.rest import Client as TwilioClient
    from twilio.twiml.voice_response import VoiceResponse, Connect
    TWILIO_AVAILABLE = True
except ImportError:
    TwilioClient = None
    VoiceResponse = None
    Connect = None
    TWILIO_AVAILABLE = False
    logger.warning("⚠️ Twilio not installed — voice call endpoints disabled.")

# ✅ Import your WhatsApp and Payment tools
from tools.notify import send_confirmation, send_interactive_slots
from tools.booking import mark_appointment_paid
from tools.booking import book_appointment, resend_payment_link
from tools.availability import check_availability
from tools.reschedule import lookup_appointment, reschedule_appointment
from tools.cancel import cancel_appointment
from tools.language import switch_language, end_call

from pipecat.frames.frames import LLMRunFrame, Frame, TextFrame
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair

from pipecat.runner.types import DailyRunnerArguments, RunnerArguments, SmallWebRTCRunnerArguments
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.daily.transport import DailyParams, DailyTransport
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

# Pipecat Services
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.sarvam.tts import SarvamTTSService
from pipecat.services.google.llm import GoogleLLMService

from db.connection import get_db_pool
from tools.pipecat_tools import init_tool_db, register_all_tools, get_tools_schema

load_dotenv(override=True)
gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# --- Global Redis Client ---
redis_client = None

async def ensure_redis_client():
    global redis_client
    if redis_client:
        return
    try:
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
        redis_client = redis.from_url(redis_url, decode_responses=True)
        await redis_client.ping()
        logger.info("✅ Redis client connected successfully.")
    except Exception as e:
        logger.warning(f"⚠️ Redis connection failed (Is it running?): {e}")
        redis_client = None

# ==========================================================
# 🌐 FASTAPI WEBHOOK SERVER SETUP
# ==========================================================
@asynccontextmanager
async def app_lifespan(app):
    # 1. Init Postgres
    pool = await get_db_pool()
    init_tool_db(pool)
    logger.info("✅ DB pool initialized for webhook server.")

    # 2. Init Redis
    await ensure_redis_client()

    yield

    if redis_client:
        await redis_client.close()

app = FastAPI(lifespan=app_lifespan)

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")

twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

class CallRequest(BaseModel):
    to_number: str

# 📞 Twilio Outbound Caller
@app.post("/make_call")
async def make_outbound_call(request: Request, data: CallRequest):
    if not twilio_client: return {"error": "Twilio not configured."}
    webhook_url = f"{request.base_url}incoming"
    try:
        call = twilio_client.calls.create(to=data.to_number, from_=TWILIO_PHONE_NUMBER, url=webhook_url)
        return {"status": "success", "call_sid": call.sid}
    except Exception as e:
        return {"error": str(e)}

# 📞 Twilio Inbound Webhook
@app.post("/incoming")
async def incoming_call(request: Request, CallSid: str = Form(None)):
    logger.info(f"📞 NEW CALL INITIATED! ID: {CallSid}")
    response = VoiceResponse()
    connect = Connect()
    wss_url = str(request.base_url).replace("http", "ws") + "media"
    
    stream = connect.stream(url=wss_url)
    if CallSid:
        stream.parameter(name="CallSid", value=CallSid)
    
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")

# 💳 Razorpay Webhook -> Marks DB Paid -> Sends WhatsApp
@app.post("/razorpay-webhook")
async def razorpay_webhook(request: Request):
    logger.info("🔔 WEBHOOK TRIGGERED: Razorpay just pinged the server!")
    
    webhook_secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
    webhook_signature = request.headers.get("X-Razorpay-Signature")
    payload_body = await request.body()
    
    if webhook_secret and webhook_signature:
        expected_signature = hmac.new(key=webhook_secret.encode(), msg=payload_body, digestmod=hashlib.sha256).hexdigest()
        if expected_signature != webhook_signature:
            logger.warning("⚠️ Webhook Failed: Invalid Razorpay Signature!")
            return {"status": "error", "message": "Invalid Signature"}

    try:
        payload = await request.json()
    except Exception:
        return {"status": "error", "message": "Invalid JSON"}
    
    if payload.get("event") == "payment_link.paid":
        payment_entity = payload["payload"]["payment_link"]["entity"]
        reference_id = payment_entity.get("reference_id", "")
        
        # Always pull the UUID from the hidden notes object first.
        notes = payment_entity.get("notes") or {}
        appt_id = notes.get("appointment_id", "UNKNOWN")
        
        # Fallback just in case.
        if appt_id == "UNKNOWN":
            parts = reference_id.split("_")
            appt_id = parts[1] if len(parts) > 1 and len(parts[1]) >= 32 else "UNKNOWN"
        
        amount_paid = payment_entity.get("amount_paid", 0) / 100
        customer_phone = payment_entity.get("customer", {}).get("contact")
        
        logger.info(f"💰 PAYMENT CONFIRMED! Appt ID: {appt_id} | Amount: ₹{amount_paid} | Phone: {customer_phone}")
        
        if appt_id != "UNKNOWN":
            await mark_appointment_paid(appt_id)
        
        if customer_phone:
            clean_phone = customer_phone.replace("+91", "").replace("+", "")
            final_msg = f"✅ Payment of ₹{amount_paid} received! Your appointment (ID: {appt_id}) is now fully CONFIRMED. Thank you for choosing Mithra Hospitals!"
            await send_confirmation(clean_phone, final_msg)
            logger.info("✅ Final WhatsApp Confirmation Sent!")

    return {"status": "success"}

# ==========================================================
# 🤖 PIPECAT VOICE AGENT
# ==========================================================

ist = pytz.timezone('Asia/Kolkata')
current_time = datetime.now(ist).strftime('%A, %B %d, %Y at %I:%M %p IST')

SYSTEM_PROMPT = f"""Role: Mithra Hospital AI Receptionist.
CURRENT LIVE TIME: {current_time}

IMPORTANT VOICE AI RULES: 
- You are a friendly, human-like hospital receptionist speaking on a phone call.
- Use short sentences. DO NOT use "..." or special characters. Use standard commas and full stops.
- Keep responses strictly under 2 sentences.
- When speaking Telugu/Hindi, you MUST respond entirely in conversational Telugu/Hindi. Use fillers like 'సరే అండి' / 'ठीक है'. 
- PRONUNCIATION CRITICAL: NEVER use numerical digits. Spell out all numbers phonetically in the language you are speaking.
- YEAR RULE: NEVER speak the year out loud to the user. However, you MUST use the correct year from the CURRENT LIVE TIME when generating timestamps for tools.

WORKFLOW:
1. Auto-Language Detection: Start by greeting in English. Listen to the user's first reply. IF they speak Telugu or Hindi, immediately call `switch_language`, then reply in their language.
2. Booking Steps (STRICT ORDER): 
   - STEP A (Check): Ask for their problem -> Call `check_availability` using the official specialty (e.g., "General Physician").
   - STEP B (Offer): Read the tool response. Tell the user the Doctor's name, specialty, and the first available time slot. Ask: "Shall I book this time for you, or do you prefer another time?"
   - STEP C (Verify Time): Check their requested time against the `available_slots` list. If unavailable, suggest the closest time. 
   - STEP D (Get Details): Once the user explicitly agrees to an available time, say EXACTLY: "Please tell me the patient name and phone number."
   - STEP E (Call Tool): Once they give their name and 10-digit phone number, call `book_appointment`. 
     * CRITICAL RULE: When you call the booking tool, do it SILENTLY. DO NOT say "Please tell me your name..." again in the same turn!
     * NEVER guess or make up names/numbers.
     * IF UNPAID WARNING: If tool returns an unpaid appointment warning, ask if they want the payment link resent or a new booking.
3. Resend Payment Link (Direct Request):
   - Get 10-digit phone -> call `resend_payment_link` -> Say: "I have resent the payment link to your WhatsApp. Please check it."
4. Wrap Up & End Call (CRITICAL):
   - After booking successfully, say: "Your appointment is booked, a payment link is sent to WhatsApp. Thank you." 
   - Wait for their response. If they say "ok", "bye", or silence, call the `end_call` tool to hang up.
5. Reschedule/Cancel: Get phone -> `lookup_appointment` -> Find new time/Confirm -> `reschedule_appointment` or `cancel_appointment`.

RULES:
- Phone numbers MUST be exactly 10 digits.
- ERROR HANDLING: If a tool returns an error, silently fix your parameters and call it again!"""

WHATSAPP_SYSTEM_PROMPT = f"""Role: Mithra Hospital AI Receptionist (WhatsApp).
CURRENT LIVE TIME: {current_time}

IMPORTANT TEXT AI RULES: 
- You are a friendly WhatsApp hospital receptionist.
- Use emojis naturally (🏥, 👨‍⚕️, 📅, etc.).
- Use standard numbers and formatting (e.g., 9:30 AM, ₹500). DO NOT spell out numbers.
- Keep responses concise and easy to read on a mobile screen.
- STRICT SEQUENCE RULE: NEVER ask for or acknowledge the patient's name or phone number until AFTER the user has selected a specific time slot. If the user provides a name/number early, IGNORE IT completely and continue with the correct step below.

WORKFLOW:
1. Auto-Language Detection: Reply in the language the user speaks.
2. Booking Steps (STRICT ORDER): 
   - STEP A (Get Problem): Ask for their medical problem or symptoms.
   - STEP B (Check): Once they state their problem -> DEDUCE THE SPECIALTY -> Call `check_availability`.
     * MAPPING EXAMPLES: "fever/cough" = "General Physician". "skin" = "Dermatologist". "heart" = "Cardiologist".
     * Pass ONLY the official specialty name to the tool.
   - STEP C (Offer Menu): When `check_availability` succeeds, the system sends a menu. YOU MUST ONLY SAY: "I have pulled up the schedule for you! Please tap the menu button above to select your preferred time ☝️"
   - STEP D (Get Details): ONLY when the user replies with a selected time (e.g., "09:30 AM"), accept the time and ask EXACTLY: "Great choice! Please provide the patient's name and 10-digit phone number."
   - STEP E (Book): Once they provide the name and phone number, call `book_appointment` IMMEDIATELY. 
     * For the `doctor_id` parameter, just pass the word "dummy_id". 
     * DO NOT call `check_availability` again.
3. Resend Payment Link: Get phone -> call `resend_payment_link`.
4. Wrap Up (CRITICAL FOR WHATSAPP):
   - When the `book_appointment` tool succeeds, say EXACTLY: "Your appointment has been tentatively booked! Please click the Razorpay link above to confirm your slot. Let me know if you need anything else!"
5. Reschedule/Cancel: Get phone -> `lookup_appointment` -> Find new time/Confirm -> `reschedule_appointment` or `cancel_appointment`.

RULES:
- Phone numbers MUST be exactly 10 digits.
- ERROR HANDLING: If a tool returns an error, silently fix your parameters and call it again!"""
class BillingTracker(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.tts_chars = 0
        self.llm_output_tokens = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TextFrame):
            text_length = len(frame.text)
            self.tts_chars += text_length
            self.llm_output_tokens += (text_length / 4.0) 
        await self.push_frame(frame, direction)

async def run_bot(transport: BaseTransport, call_sid: str = "local_test"):
    pool = await get_db_pool()
    init_tool_db(pool)
    await ensure_redis_client()

    # STT: Matching official docs with mode="transcribe"
    stt = SarvamSTTService(
        api_key=os.getenv("SARVAM_API_KEY"), 
        language="unknown", 
        model="saaras:v3",
        mode="transcribe"
    )
    
    # TTS: Set to bulbul:v2 using the correct "speaker" parameter
    tts = SarvamTTSService(
        api_key=os.getenv("SARVAM_API_KEY"), 
        target_language_code="en-IN", 
        model="bulbul:v2", 
        speaker="anushka", 
        speech_sample_rate=24000
    )
    
    llm = GoogleLLMService(api_key=os.getenv("GEMINI_API_KEY"), model="gemini-2.5-flash")

    register_all_tools(llm)
    tools = get_tools_schema()
    
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    context = LLMContext(messages=messages, tools=tools)
    context_aggregator = LLMContextAggregatorPair(context)
    billing_tracker = BillingTracker()

    pipeline = Pipeline([
        transport.input(),
        stt,
        context_aggregator.user(),
        llm,
        billing_tracker,
        tts,
        transport.output(),
        context_aggregator.assistant(),
    ])

    task = PipelineTask(
        pipeline, 
        params=PipelineParams(
            audio_in_sample_rate=16000, 
            audio_out_sample_rate=24000, 
            enable_metrics=True, 
            enable_usage_metrics=True
        )
    )
    
    call_start_time = 0

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        nonlocal call_start_time
        call_start_time = time.time()

        if redis_client:
            await redis_client.setex(f"active_call:{call_sid}", 3600, "in_progress")
            logger.info(f"🔒 Redis: Call {call_sid} locked and tracked.")

        logger.info("Client connected - triggering intro")
        messages.append({"role": "system", "content": "Say EXACTLY: 'Hi, welcome to Mithra Hospitals. How can I help you today?'"})
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected. Calculating final bill...")
        call_duration_sec = time.time() - call_start_time
        stt_cost_inr = call_duration_sec * (30.0 / 3600.0)
        tts_cost_inr = billing_tracker.tts_chars * 0.0015
        
        cumulative_input_chars = sum(len(str(msg)) for msg in context.messages if msg.get("role") == "user")
        estimated_input_tokens = cumulative_input_chars / 4.0
        llm_input_cost_inr = (estimated_input_tokens * (0.30 / 1_000_000)) * 83.0
        llm_output_cost_inr = (billing_tracker.llm_output_tokens * (2.50 / 1_000_000)) * 83.0
        total_inr = stt_cost_inr + tts_cost_inr + llm_input_cost_inr + llm_output_cost_inr

        print("\n" + "="*60)
        print("📞 CALL ENDED - TOTAL BILLING SUMMARY")
        print("-" * 60)
        print(f"💰 TOTAL CALL COST:      ₹{total_inr:.4f} INR")
        print("="*60 + "\n")

        if redis_client:
            await redis_client.delete(f"active_call:{call_sid}")
            chat_log = [m for m in context.messages if m.get("role") != "system"]
            await redis_client.setex(f"history:{call_sid}", 86400, json.dumps(chat_log))
            logger.info(f"🔓 Redis: Call {call_sid} unlocked. History saved for 24h.")
        
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)

async def bot(runner_args: RunnerArguments):
    transport = None
    match runner_args:
        case DailyRunnerArguments():
            transport = DailyTransport(runner_args.room_url, runner_args.token, "Pipecat Bot", params=DailyParams(audio_in_enabled=True, audio_out_enabled=True, audio_out_sample_rate=24000))
        case SmallWebRTCRunnerArguments():
            webrtc_connection: SmallWebRTCConnection = runner_args.webrtc_connection
            transport = SmallWebRTCTransport(webrtc_connection=webrtc_connection, params=TransportParams(audio_in_enabled=True, audio_out_enabled=True, audio_out_sample_rate=24000))
    
    if transport:
        test_call_sid = f"local_test_{uuid.uuid4().hex[:8]}"
        await run_bot(transport, call_sid=test_call_sid)

# ==========================================================
# 💬 WHATSAPP TEXT CHAT WEBHOOKS
# ==========================================================

@app.get("/whatsapp-webhook")
async def verify_whatsapp_webhook(request: Request):
    """Meta uses this to verify your server URL."""
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode and token:
        if mode == "subscribe" and token == os.getenv("WHATSAPP_VERIFY_TOKEN"):
            logger.info("✅ WhatsApp Webhook Verified by Meta!")
            # Meta requires the challenge to be returned as an integer
            return int(challenge)
        else:
            return HTMLResponse(content="Verification token mismatch", status_code=403)
    return HTMLResponse(content="Missing parameters", status_code=400)


@app.post("/whatsapp-webhook")
async def receive_whatsapp_message(request: Request):
    """Receives incoming text messages from patients."""
    try:
        body = await request.json()
        
        # Meta's JSON payload is deeply nested. We check if it's an actual message.
        if body.get("object") == "whatsapp_business_account":
            for entry in body.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    
                    if "messages" in value:
                        message = value["messages"][0]
                        sender_phone = message.get("from")
                        
                        # Extract the text OR the interactive list reply
                        incoming_text = ""
                        msg_type = message.get("type")
                        message_id = message.get("id") # 🟢 Grab the unique Meta Message ID
                        
                        if msg_type == "text":
                            incoming_text = message["text"]["body"]
                        elif msg_type == "interactive":
                            interactive_data = message.get("interactive", {})
                            if interactive_data.get("type") == "list_reply":
                                incoming_text = interactive_data["list_reply"]["title"]
                            elif interactive_data.get("type") == "button_reply":
                                incoming_text = interactive_data["button_reply"]["title"]
                        
                        # Skip if it's an image, read-receipt, or unsupported type
                        if not incoming_text:
                            continue
                            
                        # 🟢 THE FIX: Check Redis to see if we ALREADY processed this exact message ID in the last 60 seconds
                        if message_id:
                            is_duplicate = await redis_client.get(f"processed_msg:{message_id}")
                            if is_duplicate:
                                logger.info(f"⏭️ Ignored duplicate Meta retry for message: {message_id}")
                                return {"status": "success"} # Tell Meta "we got it" so they stop retrying
                            
                            # Mark it as processed for 60 seconds
                            await redis_client.setex(f"processed_msg:{message_id}", 60, "true")
                            
                        logger.info(f"💬 Incoming WhatsApp from {sender_phone}: {incoming_text}")
                        
                        # --- REPLACING THE TODO ---
                        
                        # 1. Define WhatsApp-specific Wrappers for Pipecat Tools
                        class WAParams:
                            """Dummy class to catch Pipecat callbacks in a stateless webhook."""
                            def __init__(self):
                                self.result = None
                            async def result_callback(self, result):
                                self.result = result

                        async def wa_check_availability(problem_or_speciality: str):
                            """Check doctor availability by specialty."""
                            p = WAParams()

                            # 1. Quick DB Check: Does this phone number already have a confirmed appointment today?
                            existing_booking_msg = ""
                            try:
                                pool = await get_db_pool()
                                async with pool.acquire() as conn:
                                    # Clean the phone number for the query
                                    clean_phone_check = sender_phone.replace("+91", "").replace("+", "")
                                    if clean_phone_check.startswith("91") and len(clean_phone_check) == 12:
                                        clean_phone_check = clean_phone_check[2:]

                                    check_query = """
                                        SELECT a.appointment_start, d.name as doctor_name
                                        FROM appointments a
                                        JOIN patients p ON a.patient_id = p.id
                                        JOIN doctors d ON a.doctor_id = d.id
                                        WHERE p.phone = $1
                                          AND a.status = 'confirmed'
                                          AND DATE(a.appointment_start AT TIME ZONE 'Asia/Kolkata') = CURRENT_DATE
                                        ORDER BY a.appointment_start DESC
                                        LIMIT 1
                                    """
                                    existing_appt = await conn.fetchrow(check_query, clean_phone_check)

                                    if existing_appt:
                                        appt_time = existing_appt['appointment_start'].astimezone(pytz.timezone('Asia/Kolkata')).strftime('%I:%M %p')
                                        doc_name = existing_appt['doctor_name']
                                        existing_booking_msg = f" Note: You already have a confirmed booking today at {appt_time} with {doc_name}. Proceeding to show options for your new request..."
                                        logger.info(f"ℹ️ Found existing confirmed booking for {clean_phone_check} at {appt_time}.")
                            except Exception as e:
                                logger.warning(f"⚠️ Failed to check existing appointments: {e}")

                            # 2. Proceed with the normal availability check
                            await check_availability(p, problem_or_speciality)
                            
                            result_data = p.result
                            if result_data.get("status") == "success" and result_data.get("doctors"):
                                doc = result_data["doctors"][0]
                                slots = doc.get("available_slots", [])
                                if slots:
                                    clean_phone = sender_phone.replace("+91", "").replace("+", "")
                                    await send_interactive_slots(
                                        clean_phone,
                                        doc["name"],
                                        doc["next_available_day"],
                                        slots,
                                    )

                                    # Silently save the REAL doctor ID to Redis
                                    await redis_client.setex(f"last_doc_id:{sender_phone}", 86400, doc["id"])

                                    # Append the warning if they have an existing booking!
                                    final_prompt_message = "I have sent an interactive menu to the user. Ask them to click the button to select a time."
                                    if existing_booking_msg:
                                        final_prompt_message = f"Tell the user EXACTLY: '{existing_booking_msg}' THEN say: 'Please tap the menu button above to select a time for this new appointment ☝️'"

                                    return {
                                        "status": "success", 
                                        "message": final_prompt_message,
                                        "doctors": result_data["doctors"],
                                    }
                            
                            return p.result

                        async def wa_book_appointment(doctor_id: str, patient_name: str, start_time_iso: str, phone: str, force_book: bool = False):
                            """Book an appointment."""
                            p = WAParams()

                            real_doc_id = await redis_client.get(f"last_doc_id:{sender_phone}")
                            if real_doc_id:
                                doctor_id = real_doc_id
                                logger.info(f"🛡️ Auto-corrected LLM hallucination. Using real Doc ID: {doctor_id}")

                            await book_appointment(p, doctor_id, patient_name, start_time_iso, phone, force_book)
                            return p.result

                        async def wa_resend_payment_link(phone: str):
                            """Resend the payment link to a phone number."""
                            p = WAParams()
                            await resend_payment_link(p, phone)
                            return p.result
                            
                        async def wa_lookup_appointment(phone: str):
                            """Look up an appointment by phone number."""
                            p = WAParams()
                            await lookup_appointment(p, phone)
                            return p.result

                        async def wa_reschedule_appointment(appointment_id: str, new_start_time_iso: str):
                            """Reschedule an appointment."""
                            p = WAParams()
                            await reschedule_appointment(p, appointment_id, new_start_time_iso)
                            return p.result

                        async def wa_cancel_appointment(appointment_id: str):
                            """Cancel an appointment."""
                            p = WAParams()
                            await cancel_appointment(p, appointment_id)
                            return p.result

                        async def wa_switch_language(language: str):
                            """Switch language."""
                            p = WAParams()
                            await switch_language(p, language)
                            return p.result

                        async def wa_end_call():
                            """End the call/conversation and wipe memory for next time."""
                            p = WAParams()
                            await end_call(p)
                            await redis_client.delete(f"wa_history:{sender_phone}")
                            return p.result

                        whatsapp_tools = [
                            wa_check_availability, wa_book_appointment, wa_resend_payment_link,
                            wa_lookup_appointment, wa_reschedule_appointment, wa_cancel_appointment,
                            wa_switch_language, wa_end_call
                        ]
                        
                        # 2. Fetch previous WhatsApp chat history from Redis
                        history_key = f"wa_history:{sender_phone}"
                        chat_history_str = await redis_client.get(history_key)
                        
                        chat_history = []
                        if chat_history_str:
                            raw_history = json.loads(chat_history_str)
                            for msg in raw_history:
                                # SAFEGUARD: Only load parts that actually contain text
                                valid_parts = [types.Part.from_text(text=p) for p in msg.get("parts", []) if p]
                                if valid_parts:
                                    chat_history.append(
                                        types.Content(role=msg["role"], parts=valid_parts)
                                    )

                        # 3. Append the user's new message to the list
                        chat_history.append(
                            types.Content(
                                role="user", 
                                parts=[types.Part.from_text(text=incoming_text)]
                            )
                        )

                        # 4. Start the Tool-Calling Loop
                        ai_reply = ""
                        while True:
                            response = await gemini_client.aio.models.generate_content(
                                model='gemini-2.5-flash',
                                contents=chat_history,
                                config=types.GenerateContentConfig(
                                    system_instruction=WHATSAPP_SYSTEM_PROMPT,
                                    tools=whatsapp_tools,
                                )
                            )
                            
                            # If Gemini generated text, we are done!
                            if response.text:
                                ai_reply = response.text
                                break
                            
                            # If Gemini didn't generate text, it must want to call a tool
                            if response.function_calls:
                                # Temporarily save Gemini's function call request to history
                                chat_history.append(response.candidates[0].content)
                                
                                for function_call in response.function_calls:
                                    func_name = function_call.name
                                    func_args = function_call.args
                                    logger.info(f"🛠️ WhatsApp LLM called tool: {func_name} with args {func_args}")
                                    
                                    # Map the string name to the actual Python function
                                    tool_map = {f.__name__: f for f in whatsapp_tools}
                                    if func_name in tool_map:
                                        # Execute the tool (await if it's async)
                                        try:
                                            result = await tool_map[func_name](**func_args)
                                        except TypeError:
                                            # Fallback if the tool isn't async
                                            result = tool_map[func_name](**func_args)
                                        
                                        # Feed the tool's result back into the chat history
                                        tool_result_part = types.Part.from_function_response(
                                            name=func_name,
                                            response={"result": result}
                                        )
                                        chat_history.append(
                                            types.Content(role="user", parts=[tool_result_part])
                                        )
                        
                        # 5. Save the updated history back to Redis
                        storable_history = []
                        for c in chat_history:
                            # SAFEGUARD: Extract only actual text, ignoring empty parts or raw tool data
                            text_parts = [p.text for p in c.parts if p.text]
                            if text_parts:
                                storable_history.append({"role": c.role, "parts": text_parts})
                                
                        storable_history.append({"role": "model", "parts": [ai_reply]})
                        await redis_client.setex(history_key, 86400, json.dumps(storable_history))

                        # 6. Send the reply back to the patient's WhatsApp!
                        clean_phone = sender_phone.replace("+91", "").replace("+", "")
                        await send_confirmation(clean_phone, ai_reply)
                        logger.info(f"🤖 Sent WhatsApp reply to {clean_phone}: {ai_reply}")
                            
        return {"status": "success"}
    except Exception as e:
        logger.error(f"❌ WhatsApp Webhook Error: {e}")
        return {"status": "error"}

# ==========================================================
# 🚀 LAUNCH MECHANISM (Supports both Webhooks & Pipecat CLI)
# ==========================================================
if __name__ == "__main__":
    import sys

    runner_flags = {
        "-u", "--url", "-t", "--transport", "-d", "--direct",
        "--host", "--port", "-x", "--proxy", "-f", "--folder",
        "-v", "--verbose", "--dialin", "--esp32", "--whatsapp",
    }

    if any(arg in runner_flags for arg in sys.argv[1:]):
        argv = sys.argv[1:]
        transport = None
        for index, arg in enumerate(argv):
            if arg in ("-t", "--transport") and index + 1 < len(argv):
                transport = argv[index + 1]
                break

        has_host = any(arg.startswith("--host") or arg == "--host" for arg in argv)
        if transport == "webrtc" and not has_host:
            sys.argv.extend(["--host", "127.0.0.1"])

        from pipecat.runner.run import main
        main()
    else:
        # Otherwise, spin up the FastAPI server for production Twilio/Razorpay webhooks
        print("🚀 Starting Production Webhook Server on Port 8000...")
        uvicorn.run("agent:app", host="0.0.0.0", port=8000, reload=True)