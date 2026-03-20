import os
import re
import json
import uuid
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import pytz
import redis.asyncio as redis
from dotenv import load_dotenv
from loguru import logger

# ✅ FastAPI Imports
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
    logger.warning("⚠️ Twilio not installed — phone call endpoints disabled.")

# ✅ Pipecat Imports
from pipecat.frames.frames import LLMRunFrame, Frame, TextFrame, CancelFrame, TranscriptionFrame
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
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.sarvam.tts import SarvamTTSService
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema

# ✅ Internal Tools
from db.connection import get_db_pool
from tools.pool import init_tool_db
from tools.availability import check_availability
from tools.booking import book_appointment as base_book_appointment, cancel_unpaid_appointment
from tools.reschedule import lookup_appointment
from tools.language import switch_language, end_call

load_dotenv(override=True)

# --- Global Redis Client ---
redis_client = None

async def ensure_redis_client():
    global redis_client
    if redis_client: return
    try:
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
        redis_client = redis.from_url(redis_url, decode_responses=True)
        await redis_client.ping()
        logger.info("✅ Redis client connected successfully.")
    except Exception as e:
        logger.warning(f"⚠️ Redis connection failed: {e}")
        redis_client = None

@asynccontextmanager
async def app_lifespan(app):
    pool = await get_db_pool()
    init_tool_db(pool)
    await ensure_redis_client()
    yield
    if redis_client: await redis_client.close()

app = FastAPI(lifespan=app_lifespan)

@app.post("/incoming")
async def incoming_call(request: Request, CallSid: str = Form(None)):
    logger.info(f"📞 NEW CALL INITIATED! ID: {CallSid}")
    response = VoiceResponse()
    connect = Connect()
    wss_url = str(request.base_url).replace("http", "ws") + "media"
    if CallSid: connect.stream(url=wss_url).parameter(name="CallSid", value=CallSid)
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")

# ==========================================================
# 🧠 SMART INTERCEPT WRAPPER (FOR VOICE)
# ==========================================================
async def voice_book_appointment(params: FunctionCallParams, doctor_id: str, patient_name: str, start_time_iso: str, phone: str, reason: str, force_book: bool = False, is_followup: str = "unknown"):
    clean_phone = "".join(filter(str.isdigit, str(phone)))
    if clean_phone.startswith("91") and len(clean_phone) == 12: clean_phone = clean_phone[2:]
    
    start_time_iso = start_time_iso.replace("Z", "+05:30") if "Z" in start_time_iso else start_time_iso + "+05:30" if "+" not in start_time_iso else start_time_iso
    
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            if not force_book:
                upcoming_query = """
                    SELECT a.appointment_start, d.name as doctor_name FROM appointments a
                    JOIN patients p ON a.patient_id = p.id JOIN doctors d ON a.doctor_id = d.id
                    WHERE p.phone = $1 AND a.status IN ('confirmed', 'pending') AND a.appointment_start >= NOW()
                    ORDER BY a.appointment_start ASC LIMIT 1
                """
                upcoming_appt = await conn.fetchrow(upcoming_query, clean_phone)
                if upcoming_appt:
                    appt_time = upcoming_appt['appointment_start'].astimezone(pytz.timezone('Asia/Kolkata')).strftime('%b %d at %I:%M %p')
                    doc_name = upcoming_appt['doctor_name']
                    logger.warning(f"🛑 SMART INTERCEPT: DB found existing upcoming appointment for this phone on {appt_time}!")
                    await params.result_callback({"status": "warning", "message": f"CRITICAL INSTRUCTION: Tell the user: 'I see you already have an upcoming appointment on {appt_time} with {doc_name}. Do you want to proceed with booking an additional new appointment?'"})
                    return

            followup_query = """
                SELECT a.appointment_start, d.name as doctor_name, p.name as patient_name 
                FROM appointments a JOIN patients p ON a.patient_id = p.id JOIN doctors d ON a.doctor_id = d.id
                WHERE p.phone = $1 AND a.status = 'confirmed' AND a.appointment_start >= NOW() - INTERVAL '7 days' AND a.appointment_start < NOW()
                ORDER BY a.appointment_start DESC LIMIT 1
            """
            has_recent = await conn.fetchrow(followup_query, clean_phone)

            if is_followup == "unknown":
                if has_recent:
                    recent_date = has_recent['appointment_start'].astimezone(pytz.timezone('Asia/Kolkata')).strftime('%B %d')
                    recent_patient = has_recent['patient_name']
                    recent_doc = has_recent['doctor_name']
                    logger.warning(f"🛑 SMART INTERCEPT: DB found past 7-day appointment for {recent_patient}. Prompting for free follow-up.")
                    await params.result_callback({"status": "warning", "message": f"CRITICAL INSTRUCTION: Tell the user: 'I see {recent_patient} had a confirmed appointment with {recent_doc} on {recent_date}. Is this a free 1-week follow-up for that visit, or a completely new medical problem?'"})
                    return
                else: is_followup_bool = False
            elif is_followup == "yes":
                if has_recent: is_followup_bool = True
                else:
                    await params.result_callback({"status": "warning", "message": "CRITICAL INSTRUCTION: Tell the user: 'Your free 1-week follow-up period has expired, or no previous record was found. I will need to book this as a new paid consultation. Shall I proceed?'"})
                    return
            else:
                is_followup_bool = False

    except Exception as e: logger.warning(f"⚠️ DB Error: {e}")

    original_callback = params.result_callback
    async def intercepted_callback(result):
        if result.get("status") == "success" and not result.get("is_followup"):
            appt_id = result.get("appointment_id")
            if appt_id: asyncio.create_task(cancel_unpaid_appointment(appt_id))
        await original_callback(result)
    
    params.result_callback = intercepted_callback
    await base_book_appointment(params, doctor_id, patient_name, start_time_iso, phone, reason, force_book, is_followup_bool, chatting_phone=phone)

# ==========================================================
# 🛠️ PROCESSORS 
# ==========================================================
class STTTextCleanerProcessor(FrameProcessor):
    """Logs raw STT text to the terminal and fixes typos."""
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            text = frame.text.strip().lower()
            if text: logger.info(f"🎤 USER SAID [Raw STT]: {text}")
            corrections = {
                "పార్లమెంట్": "అపాయింట్మెంట్", "apartment": "appointment",
                "అపార్ట్మెంట్": "అపాయింట్మెంట్", "department": "appointment",
                "తెలుగు": "telugu", "हिंदी": "hindi"
            }
            for k, v in corrections.items(): text = text.replace(k, v)
            frame.text = text
        await self.push_frame(frame, direction)

class BillingTracker(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.tts_chars = 0
        self.llm_output_tokens = 0
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TextFrame):
            self.tts_chars += len(frame.text)
            self.llm_output_tokens += (len(frame.text) / 4.0) 
        await self.push_frame(frame, direction)

# ==========================================================
# 🛠️ SCHEMAS & PROMPTS
# ==========================================================
def get_voice_tools_schema():
    check_availability_schema = FunctionSchema(
        name="check_availability",
        description="Checks doctor availability by specialty.",
        properties={
            "problem_or_speciality": {
                "type": "string", 
                "description": "CRITICAL: You must map the user's symptoms to one of our exact official database titles: 'General Physician', 'Dermatologist', 'Cardiologist', 'Pediatrician', 'Orthopedic', or 'Urologist'."
            },
            "requested_date": {
                "type": "string", 
                "description": "Optional (YYYY-MM-DD). ONLY provide if the user specifies a day. If they ask for 'next available', leave this blank."
            }
        },
        required=["problem_or_speciality"]
    )
    book_appointment_schema = FunctionSchema(
        name="voice_book_appointment",
        description="Books the appointment.",
        properties={
            "doctor_id": {"type": "string"},
            "patient_name": {"type": "string"},
            "start_time_iso": {"type": "string"},
            "phone": {"type": "string"},
            "reason": {"type": "string"},
            "force_book": {"type": "boolean"},
            "is_followup": {"type": "string", "description": "'unknown' by default. Pass 'yes' if user confirms it is a 7-day free follow-up, 'no' if it is a new issue."}
        },
        required=["doctor_id", "patient_name", "start_time_iso", "phone", "reason"]
    )
    return ToolsSchema(standard_tools=[check_availability_schema, book_appointment_schema, lookup_appointment, switch_language, end_call])

ist = pytz.timezone('Asia/Kolkata')
current_time = datetime.now(ist).strftime('%A, %B %d, %Y at %I:%M %p IST')

SYSTEM_PROMPT = f"""Role: Mithra Hospital AI Receptionist.
CURRENT LIVE TIME: {current_time}

CRITICAL BEHAVIOR RULES (READ CAREFULLY):
1. STRICT STATE MACHINE: You must move forward in states. NEVER go backward to an earlier state once it is completed.
2. ONE-CLICK CONFIRMATION: If the user says "yes", "okay", or "book it" to a suggested slot, you MUST immediately move to STATE 3. DO NOT re-read the apology or the slot details.
3. SILENCE ON EXECUTION: When you receive the Name and Number, trigger `voice_book_appointment` SILENTLY. You are FORBIDDEN from saying "Could you please tell me..." or any conversational filler while the tool is running.
4. NO PLEASANTRIES: Do not say "Hi", "Hello", or "I'm here to help". 
5. TIME FORMATTING: Write "9 AM" (not 09:00 AM) so the voice engine reads it naturally.

CONVERSATION STATES:
- STATE 1 (Symptoms): Ask "What medical problem are you experiencing?" STOP and wait. 
  * Once they reply, call `check_availability` SILENTLY.
- STATE 2 (The Offer): 
  * IF NOT AVAILABLE TODAY: Say: "I apologize, Dr. [Name] is not available today. Their next available slot is on [Date] at [Time]. Shall I book this for you?" STOP and wait.
  * IF USER SAYS YES: Immediately jump to STATE 3. DO NOT repeat this apology.
- STATE 3 (Details): Ask EXACTLY once: "Could you please tell me the patient's name and 10-digit phone number?" STOP and wait.
- STATE 4 (Execution): The moment the user provides Name/Number, call `voice_book_appointment` SILENTLY with 0 characters of text.
  * Prohibited: Repeating the question from STATE 3.
  * If a Warning (Double Booking) occurs: Read the warning exactly and wait for user reply.
- STATE 5 (Wrap Up): If booking succeeds, say: "Your appointment is booked. A payment link has been sent to your WhatsApp. Please pay within 15 minutes to confirm." STOP AND WAIT.
- STATE 6 (Hang Up): ONLY call `end_call` AFTER the user says "Okay", "Thank you", or "Bye".

Reschedule/Cancel: AI cannot change confirmed bookings. Tell them to call the clinic."""

# ==========================================================
# 🎙️ PIPECAT RUNNER (Restored to Single Stable Pipeline)
# ==========================================================
async def run_bot(transport: BaseTransport, call_sid: str = "local_test"):
    pool = await get_db_pool()
    init_tool_db(pool)
    await ensure_redis_client()

    stt = SarvamSTTService(api_key=os.getenv("SARVAM_API_KEY"), language="unknown", model="saaras:v3", mode="transcribe")
    
    # 🟢 Restored to the single, stable TTS engine
    tts = SarvamTTSService(api_key=os.getenv("SARVAM_API_KEY"), target_language_code="en-IN", model="bulbul:v2", speaker="anushka", speech_sample_rate=24000)
    
    llm = GoogleLLMService(api_key=os.getenv("GEMINI_API_KEY"), model="gemini-2.5-flash")

    llm.register_direct_function(check_availability, cancel_on_interruption=False)
    llm.register_direct_function(voice_book_appointment, cancel_on_interruption=False)
    llm.register_direct_function(lookup_appointment, cancel_on_interruption=False)
    llm.register_direct_function(switch_language, cancel_on_interruption=False)
    llm.register_direct_function(end_call, cancel_on_interruption=False)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    context = LLMContext(messages=messages, tools=get_voice_tools_schema())
    context_aggregator = LLMContextAggregatorPair(context)
    
    billing_tracker = BillingTracker()

    pipeline = Pipeline([
        transport.input(), 
        stt, 
        STTTextCleanerProcessor(),
        context_aggregator.user(), 
        llm, 
        billing_tracker,
        tts,
        transport.output(), 
        context_aggregator.assistant()
    ])
    
    task = PipelineTask(pipeline, params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000, enable_metrics=True, enable_usage_metrics=True))

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        if redis_client: 
            await redis_client.setex(f"active_call:{call_sid}", 3600, "in_progress")
        
        # This will now ONLY play once through the single TTS engine!
        greeting = "Hi, welcome to Mithra Hospitals. How can I help you today?"
        messages.append({"role": "assistant", "content": greeting})
        await task.queue_frames([TextFrame(greeting)])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        if redis_client:
            await redis_client.delete(f"active_call:{call_sid}")
            await redis_client.setex(f"history:{call_sid}", 86400, json.dumps([m for m in context.messages if m.get("role") != "system"]))
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)

async def bot(runner_args: RunnerArguments):
    transport = None
    match runner_args:
        case DailyRunnerArguments(): transport = DailyTransport(runner_args.room_url, runner_args.token, "Pipecat Bot", params=DailyParams(audio_in_enabled=True, audio_out_enabled=True, audio_out_sample_rate=24000))
        case SmallWebRTCRunnerArguments(): transport = SmallWebRTCTransport(webrtc_connection=runner_args.webrtc_connection, params=TransportParams(audio_in_enabled=True, audio_out_enabled=True, audio_out_sample_rate=24000))
    if transport: await run_bot(transport, call_sid=f"local_test_{uuid.uuid4().hex[:8]}")

if __name__ == "__main__":
    import sys
    runner_flags = {"-u", "--url", "-t", "--transport", "-d", "--direct", "--host", "--port", "-x", "--proxy", "-f", "--folder", "-v", "--verbose", "--dialin", "--esp32", "--webrtc"}
    argv = sys.argv[1:]
    
    if "--webrtc" in argv and not any(arg in ("-t", "--transport") for arg in argv):
        argv = [arg for arg in argv if arg != "--webrtc"]
        argv.extend(["--transport", "webrtc"])

    if any(arg in runner_flags for arg in argv):
        transport = None
        for index, arg in enumerate(argv):
            if arg in ("-t", "--transport") and index + 1 < len(argv):
                transport = argv[index + 1]
                break
        has_host = any(arg.startswith("--host") or arg == "--host" for arg in argv)
        if transport == "webrtc" and not has_host: argv.extend(["--host", "127.0.0.1"])
        sys.argv = [sys.argv[0], *argv]

        from pipecat.runner.run import main
        main()
    else:
        print("🚀 Starting Twilio Call Server on Port 8000...")
        uvicorn.run("call_agent:app", host="0.0.0.0", port=8000, reload=True)