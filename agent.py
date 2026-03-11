#tts with gemini
# import os
# import time
# from datetime import datetime
# import pytz
# from dotenv import load_dotenv
# from loguru import logger

# from pipecat.frames.frames import LLMRunFrame, Frame, TextFrame
# from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
# from pipecat.pipeline.pipeline import Pipeline
# from pipecat.pipeline.runner import PipelineRunner
# from pipecat.pipeline.task import PipelineParams, PipelineTask
# from pipecat.processors.aggregators.llm_context import LLMContext
# from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair

# from pipecat.runner.types import DailyRunnerArguments, RunnerArguments, SmallWebRTCRunnerArguments
# from pipecat.transports.base_transport import BaseTransport, TransportParams
# from pipecat.transports.daily.transport import DailyParams, DailyTransport
# from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
# from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

# from pipecat.services.sarvam.stt import SarvamSTTService
# from pipecat.services.google.llm import GoogleLLMService
# from pipecat.services.google.tts import GoogleTTSService

# from db.connection import get_db_pool
# from tools.pipecat_tools import init_tool_db, register_all_tools, get_tools_schema

# load_dotenv(override=True)

# ist = pytz.timezone('Asia/Kolkata')
# current_time = datetime.now(ist).strftime('%A, %B %d at %I:%M %p IST')

# SYSTEM_PROMPT = f"""Role: Mithra Hospital AI Receptionist.
# CURRENT LIVE TIME: {current_time} (NEVER mention the year).

# IMPORTANT VOICE AI RULES: 
# - You are a friendly, human-like hospital receptionist speaking on a phone call.
# - Use short sentences. DO NOT use "..." or special characters. Use standard commas and full stops.
# - Keep responses strictly under 2 sentences.
# - When speaking Telugu, you MUST respond entirely in conversational Telugu. Use Telugu fillers like 'సరే అండి', 'ఒక్క నిమిషం'. 
# - TELUGU PRONUNCIATION CRITICAL: NEVER use numerical digits (like 10). Spell out all numbers using Telugu words (e.g., 'పది గంటలకు').

# WORKFLOW:
# 1. Lang Switch: If user speaks Hindi/Telugu, call `switch_language` then reply in that language.
# 2. Book: 
#    - Ask problem.
#    - DEDUCE SPECIALTY: Figure out the official medical specialty (e.g., "fever and cold" = "General Physician").
#    - CALL TOOL: Call `check_availability` using ONLY the official specialty name.
#    - WHEN YOU GET DOCTOR DETAILS: Read their `working_hours` and `first_slot` to the user.
#      (Example in Telugu: "జనరల్ ఫిజిషియన్ డాక్టర్ రోహన్ శర్మ గారు సోమవారం ఉదయం 9 నుండి సాయంత్రం 5 గంటల వరకు అందుబాటులో ఉంటారు. మొదటి స్లాట్ ఉదయం 9 గంటలకు ఉంది.")
#    - FLEXIBLE BOOKING: If the user asks for a specific time (like 10 AM or 2 PM) within the `working_hours`, ACCEPT IT. You do not have to book the first slot.
#    - LUNCH BREAK RULE (CRITICAL): The doctor has a lunch break from 12:00 PM to 1:00 PM. If the user asks for a slot between 12 and 1, you MUST reject it.
#      (Say in Telugu: "క్షమించండి, 12 నుండి 1 గంటల వరకు డాక్టరు గారికి లంచ్ బ్రేక్ అండి. ఆ సమయంలో కుదరదు, వేరే సమయం చెప్పండి.")
#    - Ask for name & 10-digit phone -> Repeat phone.
#    - Call `book_appointment`. 
#      -> CRITICAL: `doctor_id` MUST be the exact 36-character UUID from the results. NEVER invent a UUID.
#    - Call `send_payment_link`.
# 3. Reschedule: Get phone -> `lookup_appointment` -> Find new time -> `reschedule_appointment`.
# 4. Cancel: Get phone -> `lookup_appointment` -> Confirm -> `cancel_appointment`.

# RULES:
# - Phone numbers MUST be exactly 10 digits.
# - ERROR HANDLING: If a tool returns an error, DO NOT tell the user about the internal error. Silently fix your parameters and call the tool again immediately!"""
# # ==========================================================
# # 💰 UNIFIED BILLING TRACKER
# # ==========================================================
# class BillingTracker(FrameProcessor):
#     def __init__(self):
#         super().__init__()
#         self.tts_chars = 0
#         self.llm_output_tokens = 0

#     async def process_frame(self, frame: Frame, direction: FrameDirection):
#         await super().process_frame(frame, direction)
        
#         # TextFrame is emitted by the LLM before it hits the TTS
#         if isinstance(frame, TextFrame):
#             text_length = len(frame.text)
#             self.tts_chars += text_length
#             # 1 Token is roughly 4 characters
#             self.llm_output_tokens += (text_length / 4.0) 
            
#         await self.push_frame(frame, direction)

# # ==========================================================
# # 🤖 BOT LOGIC & PIPELINE
# # ==========================================================
# async def run_bot(transport: BaseTransport):
#     pool = await get_db_pool()
#     init_tool_db(pool)

#     stt = SarvamSTTService(
#         api_key=os.getenv("SARVAM_API_KEY"), 
#         language="unknown", 
#         model="saaras:v3", 
#         mode="transcribe"
#     )
    
#     tts = GoogleTTSService(
#         voice="en-US-Chirp3-HD-Despina"
#     )
    
#     llm = GoogleLLMService(
#         api_key=os.getenv("GEMINI_API_KEY"), 
#         model="gemini-2.5-flash"
#     )

#     register_all_tools(llm)
#     tools = get_tools_schema()
    
#     messages = [{"role": "system", "content": SYSTEM_PROMPT}]
#     context = LLMContext(messages=messages, tools=tools)
#     context_aggregator = LLMContextAggregatorPair(context)

#     # Initialize the Tracker
#     billing_tracker = BillingTracker()

#     pipeline = Pipeline([
#         transport.input(),
#         stt,
#         context_aggregator.user(),
#         llm,
#         billing_tracker,  # <-- Intercepts text between LLM and TTS
#         tts,
#         transport.output(),
#         context_aggregator.assistant(),
#     ])

#     task = PipelineTask(
#         pipeline, 
#         params=PipelineParams(
#             audio_in_sample_rate=16000,
#             audio_out_sample_rate=24000,
#             enable_metrics=True,
#             enable_usage_metrics=True,
#         )
#     )

#     # Variable to hold the exact time the user connects
#     call_start_time = 0

#     @transport.event_handler("on_client_connected")
#     async def on_client_connected(transport, client):
#         nonlocal call_start_time
#         call_start_time = time.time()  # Start the STT clock!
        
#         logger.info("Client connected - triggering intro")
#         messages.append({
#             "role": "system", 
#             "content": "Say EXACTLY: 'Welcome to Mithra Hospitals. Let's continue in English, Telugu, or Hindi.'"
#         })
#         await task.queue_frames([LLMRunFrame()])

#     @transport.event_handler("on_client_disconnected")
#     async def on_client_disconnected(transport, client):
#         logger.info("Client disconnected. Calculating final bill...")
        
#         # 1. STT Calculation (Sarvam: ₹30/hr)
#         call_duration_sec = time.time() - call_start_time
#         stt_cost_inr = call_duration_sec * (30.0 / 3600.0)
#         stt_cost_usd = stt_cost_inr / 83.0

#         # 2. TTS Calculation (Google Chirp 3 HD: $30/1M chars)
#         tts_cost_usd = billing_tracker.tts_chars * 0.00003
#         tts_cost_inr = tts_cost_usd * 83.0

#         # 3. LLM Context Window Calculation (Gemini 2.5 Flash Input)
#         cumulative_input_chars = 0
#         current_history_chars = 0
        
#         # LLMs receive the ENTIRE chat history on every single turn.
#         for msg in context.messages:
#             # Safely stringify the message dictionary to count characters
#             msg_length = len(str(msg))
#             current_history_chars += msg_length
#             # If this is a user message, it means the LLM processed a new turn
#             if msg.get("role") == "user":
#                 cumulative_input_chars += current_history_chars
                
#         estimated_input_tokens = cumulative_input_chars / 4.0
#         llm_input_cost_usd = estimated_input_tokens * (0.30 / 1_000_000)
#         llm_input_cost_inr = llm_input_cost_usd * 83.0

#         # 4. LLM Output Calculation (Gemini 2.5 Flash Output)
#         llm_output_cost_usd = billing_tracker.llm_output_tokens * (2.50 / 1_000_000)
#         llm_output_cost_inr = llm_output_cost_usd * 83.0

#         # 5. Grand Totals
#         total_usd = stt_cost_usd + tts_cost_usd + llm_input_cost_usd + llm_output_cost_usd
#         total_inr = total_usd * 83.0

#         # 🧾 PRINT RECEIPT
#         print("\n" + "="*60)
#         print("📞 CALL ENDED - TOTAL BILLING SUMMARY")
#         print("-" * 60)
#         print(f"⏱️  Call Duration:       {call_duration_sec:.1f} seconds")
#         print(f"🎙️  STT (Sarvam):        ${stt_cost_usd:.5f}  (₹{stt_cost_inr:.3f})")
#         print(f"🧠  LLM In (Gemini):     ${llm_input_cost_usd:.5f}  (₹{llm_input_cost_inr:.3f}) [~{int(estimated_input_tokens)} tokens]")
#         print(f"💡  LLM Out (Gemini):    ${llm_output_cost_usd:.5f}  (₹{llm_output_cost_inr:.3f}) [~{int(billing_tracker.llm_output_tokens)} tokens]")
#         print(f"🗣️  TTS (Google):        ${tts_cost_usd:.5f}  (₹{tts_cost_inr:.3f}) [{billing_tracker.tts_chars} chars]")
#         print("-" * 60)
#         print(f"💰 TOTAL CALL COST:      ${total_usd:.5f} USD  (₹{total_inr:.3f} INR)")
#         print("="*60 + "\n")
        
#         await task.cancel()

#     runner = PipelineRunner(handle_sigint=False)
#     await runner.run(task)

# async def bot(runner_args: RunnerArguments):
#     transport = None
#     match runner_args:
#         case DailyRunnerArguments():
#             transport = DailyTransport(
#                 runner_args.room_url,
#                 runner_args.token,
#                 "Pipecat Bot",
#                 params=DailyParams(audio_in_enabled=True, audio_out_enabled=True, audio_out_sample_rate=24000),
#             )
#         case SmallWebRTCRunnerArguments():
#             webrtc_connection: SmallWebRTCConnection = runner_args.webrtc_connection
#             transport = SmallWebRTCTransport(
#                 webrtc_connection=webrtc_connection,
#                 params=TransportParams(audio_in_enabled=True, audio_out_enabled=True, audio_out_sample_rate=24000),
#             )
#         case _:
#             logger.error(f"Unsupported runner arguments type: {type(runner_args)}")
#             return
#     await run_bot(transport)

# if __name__ == "__main__":
#     from pipecat.runner.run import main
#     main()

import os
import time
from datetime import datetime
import pytz
from dotenv import load_dotenv
from loguru import logger

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

from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.sarvam.tts import SarvamTTSService
from pipecat.services.google.llm import GoogleLLMService

from db.connection import get_db_pool
from tools.pipecat_tools import init_tool_db, register_all_tools, get_tools_schema

load_dotenv(override=True)

ist = pytz.timezone('Asia/Kolkata')
# Added %Y so the LLM knows the current year for the database timestamp!
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
   - Ask problem.
   - DEDUCE SPECIALTY: You MUST map symptoms to official specialties BEFORE calling the tool (e.g., "fever/cold/cough" = "General Physician").
   - CALL TOOL: Call `check_availability` using ONLY the official specialty name.
   - DATE VERIFICATION & DOCTOR DETAILS (CRITICAL): 
     * YOU MUST ALWAYS STATE THE DOCTOR'S NAME AND SPECIALTY (e.g., "General Physician, Dr. Rohan Sharma").
     * Read the `next_available_day` from the tool carefully. If it is tomorrow or a future date, explicitly state they are NOT available today.
     * DO NOT mention the full working hours.
     * English Example: "General Physician, Dr. Rohan Sharma is available today. The first available slot is at 1:30 PM. Shall I book this for you?"
   - SLOT VERIFICATION (SUPER CRITICAL): 
     * YOU MUST READ the `available_slots` list from the tool.
     * IF REQUESTED TIME IS NOT IN THE LIST: DO NOT AGREE. Say it is booked/lunch and suggest closest alternative times! 
     * IF REQUESTED TIME IS IN THE LIST: Accept it, and proceed to the next step.
   - INFORMATION GATHERING (HARD STOP): YOU MUST explicitly ask the user for their Name and 10-digit Phone Number. DO NOT call `book_appointment` until the user has spoken both of these details to you. NEVER guess or invent names/numbers.
   - Call `book_appointment`. -> CRITICAL: Use exact 36-character UUID from results.
     * WARNING CHECK: If the tool returns a warning that the user already has an appointment, YOU MUST ASK THEM: "You already have an appointment booked at [Time]. Are you sure you want to book a new one?"
     * IF THEY SAY YES: Call the `book_appointment` tool again, but this time set `force_book` to true!
     * IF THEY SAY NO: Acknowledge and end the call.
3. Wrap Up & End Call (CRITICAL):
   - After booking, say: "Your appointment is booked, a payment link is sent to WhatsApp. Thank you." (Translate to spoken language).
   - Wait for their response. If they say "ok", "thank you", "bye", or silence, call the `end_call` tool to hang up.
4. Reschedule/Cancel: Get phone -> `lookup_appointment` -> Find new time/Confirm -> `reschedule_appointment` or `cancel_appointment`.

RULES:
- Phone numbers MUST be exactly 10 digits.
- ERROR HANDLING: If a tool returns an error, silently fix your parameters and call it again!"""
# ==========================================================
# 💰 UNIFIED BILLING TRACKER
# ==========================================================
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

# ==========================================================
# 🤖 BOT LOGIC & PIPELINE
# ==========================================================
async def run_bot(transport: BaseTransport):
    pool = await get_db_pool()
    init_tool_db(pool)

    # 1. STT: Sarvam
    stt = SarvamSTTService(
        api_key=os.getenv("SARVAM_API_KEY"), 
        language="unknown", 
        model="saaras:v3", 
        mode="transcribe"
    )
    
    # 2. TTS: Sarvam Bulbul v2 (Anushka, 24kHz)
    tts = SarvamTTSService(
        api_key=os.getenv("SARVAM_API_KEY"), 
        target_language_code="en-IN", 
        model="bulbul:v2", 
        voice="anushka",
        speech_sample_rate=24000
    )
    
    # 3. LLM: Gemini 2.5 Flash
    llm = GoogleLLMService(
        api_key=os.getenv("GEMINI_API_KEY"), 
        model="gemini-2.5-flash"
    )

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
            enable_usage_metrics=True,
        )
    )

    call_start_time = 0

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        nonlocal call_start_time
        call_start_time = time.time()
        
        logger.info("Client connected - triggering intro")
        messages.append({
            "role": "system", 
            "content": "Say EXACTLY: 'Hi, welcome to Mithra Hospitals. How can I help you today?'"
        })
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected. Calculating final bill...")
        
        # 1. STT Calculation (Sarvam: ₹30/hr)
        call_duration_sec = time.time() - call_start_time
        stt_cost_inr = call_duration_sec * (30.0 / 3600.0)
        stt_cost_usd = stt_cost_inr / 83.0

        # 2. TTS Calculation (Sarvam Bulbul v2: ₹15 per 10K chars)
        tts_cost_inr = billing_tracker.tts_chars * 0.0015
        tts_cost_usd = tts_cost_inr / 83.0

        # 3. LLM Context Window Calculation (Gemini 2.5 Flash Input)
        cumulative_input_chars = 0
        current_history_chars = 0
        
        for msg in context.messages:
            msg_length = len(str(msg))
            current_history_chars += msg_length
            if msg.get("role") == "user":
                cumulative_input_chars += current_history_chars
                
        estimated_input_tokens = cumulative_input_chars / 4.0
        llm_input_cost_usd = estimated_input_tokens * (0.30 / 1_000_000)
        llm_input_cost_inr = llm_input_cost_usd * 83.0

        # 4. LLM Output Calculation (Gemini 2.5 Flash Output)
        llm_output_cost_usd = billing_tracker.llm_output_tokens * (2.50 / 1_000_000)
        llm_output_cost_inr = llm_output_cost_usd * 83.0

        # 5. Grand Totals
        total_inr = stt_cost_inr + tts_cost_inr + llm_input_cost_inr + llm_output_cost_inr
        total_usd = total_inr / 83.0

        # 🧾 PRINT RECEIPT
        print("\n" + "="*60)
        print("📞 CALL ENDED - TOTAL BILLING SUMMARY")
        print("-" * 60)
        print(f"⏱️  Call Duration:       {call_duration_sec:.1f} seconds")
        print(f"🎙️  STT (Sarvam):        ₹{stt_cost_inr:.4f}  (${stt_cost_usd:.5f})")
        print(f"🧠  LLM In (Gemini):     ₹{llm_input_cost_inr:.4f}  (${llm_input_cost_usd:.5f}) [~{int(estimated_input_tokens)} tokens]")
        print(f"💡  LLM Out (Gemini):    ₹{llm_output_cost_inr:.4f}  (${llm_output_cost_usd:.5f}) [~{int(billing_tracker.llm_output_tokens)} tokens]")
        print(f"🗣️  TTS (Sarvam):        ₹{tts_cost_inr:.4f}  (${tts_cost_usd:.5f}) [{billing_tracker.tts_chars} chars]")
        print("-" * 60)
        print(f"💰 TOTAL CALL COST:      ₹{total_inr:.4f} INR  (${total_usd:.5f} USD)")
        print("="*60 + "\n")
        
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)

async def bot(runner_args: RunnerArguments):
    transport = None
    match runner_args:
        case DailyRunnerArguments():
            transport = DailyTransport(
                runner_args.room_url,
                runner_args.token,
                "Pipecat Bot",
                params=DailyParams(audio_in_enabled=True, audio_out_enabled=True, audio_out_sample_rate=24000),
            )
        case SmallWebRTCRunnerArguments():
            webrtc_connection: SmallWebRTCConnection = runner_args.webrtc_connection
            transport = SmallWebRTCTransport(
                webrtc_connection=webrtc_connection,
                params=TransportParams(audio_in_enabled=True, audio_out_enabled=True, audio_out_sample_rate=24000),
            )
        case _:
            logger.error(f"Unsupported runner arguments type: {type(runner_args)}")
            return
    await run_bot(transport)

if __name__ == "__main__":
    from pipecat.runner.run import main
    main()