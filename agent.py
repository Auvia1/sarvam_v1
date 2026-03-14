import os
import time
import hmac
import hashlib
import json
import uuid
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
from tools.notify import send_confirmation
from tools.booking import mark_appointment_paid

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
        
        # Extract UUID handling format APPT_{uuid}_{timestamp}
        parts = reference_id.split("_")
        appt_id = parts[1] if len(parts) > 1 else "UNKNOWN"
        
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
- PRONUNCIATION CRITICAL: NEVER use numerical digits (like 9 or 5). Spell out all numbers phonetically in the language you are speaking.
- YEAR RULE: NEVER speak the year out loud to the user. However, you MUST use the correct year from the CURRENT LIVE TIME when generating timestamps for tools.

WORKFLOW:
1. Auto-Language Detection: Start by greeting in English. Listen to the user's first reply. IF they speak Telugu or Hindi, immediately call `switch_language`, then reply in their language. DO NOT ask what language they want.
2. Book: 
   - Ask problem -> DEDUCE SPECIALTY -> Call `check_availability` using ONLY the official specialty name.
     * SPECIALTY EXAMPLES: "fever/cold" = "General Physician", "skin/pimples/rash" = "Dermatologist".
     - NO DOCTORS FOUND: If the tool returns an error that no doctors were found, DO NOT make up dates. Apologize, state the specialty is unavailable, and ask if they want you to check for a General Physician. If they say yes, you MUST call `check_availability` again using "General Physician" before stating any day/time.
   - DATE VERIFICATION & DOCTOR DETAILS (CRITICAL): 
     * YOU MUST ALWAYS STATE THE DOCTOR'S NAME AND SPECIALTY.
         * Read `is_available_today` and `next_available_day` from the tool carefully.
         * If `is_available_today` is false, you MUST explicitly say they are NOT available today.
         * NEVER say "available today" unless `is_available_today` is true in the tool response.
     * English Example: "General Physician, Dr. Rohan Sharma is available today. The first available slot is at 1:30 PM. Shall I book this for you?"
   - SLOT VERIFICATION (SUPER CRITICAL): 
     * YOU MUST READ the `available_slots` list from the tool.
     * IF REQUESTED TIME IS NOT IN THE LIST: DO NOT AGREE. Say it is booked/lunch and suggest closest alternative times! 
     * IF REQUESTED TIME IS IN THE LIST: Accept it, AND IMMEDIATELY ASK FOR THEIR NAME AND 10-DIGIT PHONE NUMBER.
   - INFORMATION GATHERING (HARD STOP): 
     * When the user agrees to book a slot, YOU MUST explicitly say EXACTLY this: "Please tell me the patient name and phone number."
     * DO NOT call `book_appointment` until you have HEARD the user speak both details.
     * NEVER guess, NEVER make up names (like "John Doe" or "Hari"), and NEVER use fake numbers (like "9988776655" or "1234567890").
   - Call `book_appointment`. -> CRITICAL: Use exact 36-character UUID from results.
     * UNPAID WARNING CHECK: If the tool says the user has an UNPAID appointment, ask: "You have an unpaid appointment at [Time]. Should I resend the payment link, or do you want to book a new appointment?"
       -> IF THEY SAY "RESEND": Call the `resend_payment_link` tool using their phone number!
       -> IF THEY SAY "NEW": Call `book_appointment` again with `force_book` set to true.
3. Resend Payment Link (Direct Request):
   - If the user says "I didn't get the link" or "Resend payment link", ask for their 10-digit phone number and call `resend_payment_link`.
   - After calling, say: "I have resent the payment link to your WhatsApp. Please check it."
4. Wrap Up & End Call (CRITICAL):
   - After booking or resending a link, say: "Your appointment is booked, a payment link is sent to WhatsApp. Thank you." (Translate to spoken language).
   - Wait for their response. If they say "ok", "thank you", "bye", or silence, call the `end_call` tool to hang up.
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