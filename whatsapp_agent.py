import os
import hmac
import hashlib
import json
import asyncio
from google import genai
from google.genai import types
from contextlib import asynccontextmanager
from datetime import datetime
import pytz
import redis.asyncio as redis
from dotenv import load_dotenv
from loguru import logger

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from tools.notify import send_confirmation, send_interactive_slots
from tools.booking import mark_appointment_paid, cancel_unpaid_appointment, book_appointment
from tools.availability import check_availability
from tools.reschedule import lookup_appointment
from tools.language import switch_language, end_call
from db.connection import get_db_pool
from tools.pool import init_tool_db 

load_dotenv(override=True)
gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
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

@app.post("/razorpay-webhook")
async def razorpay_webhook(request: Request):
    logger.info("🔔 WEBHOOK TRIGGERED: Razorpay pinged the server!")
    webhook_secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
    webhook_signature = request.headers.get("X-Razorpay-Signature")
    payload_body = await request.body()
    
    if webhook_secret and webhook_signature:
        expected_signature = hmac.new(key=webhook_secret.encode(), msg=payload_body, digestmod=hashlib.sha256).hexdigest()
        if expected_signature != webhook_signature: return {"status": "error", "message": "Invalid Signature"}

    try: payload = await request.json()
    except: return {"status": "error", "message": "Invalid JSON"}
    
    if payload.get("event") == "payment_link.paid":
        payment_entity = payload["payload"]["payment_link"]["entity"]
        reference_id = payment_entity.get("reference_id", "")
        notes = payment_entity.get("notes") or {}
        appt_id = notes.get("appointment_id", "UNKNOWN")
        
        if appt_id == "UNKNOWN":
            parts = reference_id.split("_")
            appt_id = parts[1] if len(parts) > 1 and len(parts[1]) >= 32 else "UNKNOWN"
        
        amount_paid = payment_entity.get("amount_paid", 0) / 100
        customer_phone = payment_entity.get("customer", {}).get("contact")
        
        if appt_id != "UNKNOWN": await mark_appointment_paid(appt_id)
        
        # 🟢 NEW: Pull the chatting phone from Redis and send to BOTH numbers
        chatting_phone = None
        if appt_id != "UNKNOWN" and redis_client:
            chatting_phone = await redis_client.get(f"appt_chat:{appt_id}")
            
        clean_customer = customer_phone.replace("+91", "").replace("+", "") if customer_phone else None
        
        # The set() automatically removes duplicates if clean_customer == chatting_phone
        target_phones = {p for p in [clean_customer, chatting_phone] if p}
        
        final_msg = f"✅ Payment of ₹{amount_paid} received! Your appointment is now fully CONFIRMED. Thank you!"
        
        for target in target_phones:
            await send_confirmation(target, final_msg)

    return {"status": "success"}

# ==========================================================
# 🤖 WHATSAPP PROMPT
# ==========================================================
ist = pytz.timezone('Asia/Kolkata')
current_time = datetime.now(ist).strftime('%A, %B %d, %Y at %I:%M %p IST')

WHATSAPP_SYSTEM_PROMPT = f"""Role: Mithra Hospital AI Receptionist (WhatsApp).
CURRENT LIVE TIME: {current_time}

CRITICAL BEHAVIOR RULES (READ CAREFULLY):
1. ANTI-REPETITION: NEVER repeat a greeting (like "Welcome to Mithra Hospital") if you have already said it in the chat history. 
2. GREET ONLY ONCE: Only say "Welcome to Mithra Hospital" in your very first message.
3. SHORT & NATURAL: Keep responses concise and easy to read on a mobile screen. Use emojis naturally.
4. NO PREMATURE DETAILS: NEVER ask for the patient's name or phone number until AFTER they select a specific time slot.

WORKFLOW (Execute strictly ONE step at a time):
- STEP A (Symptoms): Ask what medical problem they are experiencing. STOP and wait.
- STEP B (Check): DEDUCE THE SPECIALTY -> Call `check_availability`. 
  * Leave `requested_date` BLANK for "next available" searches.
- STEP C (Offer Menu): 
  * IF AVAILABLE TODAY (`is_available_today`: true): Say ONLY "I have pulled up the schedule for you! Please tap the menu button above to select your preferred time ☝️"
  * IF NOT AVAILABLE TODAY (`is_available_today`: false): Say EXACTLY "Our [Specialty] is not available today. Shall I check if a General Physician is available?" DO NOT send the menu. DO NOT tell them the next date unless they explicitly reply asking "when are they available next". 
  * IF THEY ASK WHEN NEXT: Then say "The next available day is [Date]. Shall I send the available time slots?"
- STEP D (Get Details): Once they select a time, ask EXACTLY: "Great choice! Please provide the patient's name and 10-digit phone number." STOP AND WAIT.
- STEP E (Book): ONLY call `book_appointment` AFTER the user texts their real name and phone number. 
  * For `doctor_id`, pass "dummy_id". Pass the `reason`.
  * IF WARNING: Stop and ask the user exactly what the tool tells you to ask.
  * If follow-up, call tool again with `is_followup`="yes". If new, pass "no".
- STEP F (Wrap Up): Output EXACTLY the wrap-up message the tool provides.

Reschedule/Cancel Policy: Inform them confirmed appointments cannot be changed via AI. Call the clinic.
ERROR HANDLING: If a tool returns an error, silently fix parameters and call it again!"""
# ==========================================================
# 💬 WHATSAPP TEXT CHAT WEBHOOKS
# ==========================================================
@app.get("/whatsapp-webhook")
async def verify_whatsapp_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and token == os.getenv("WHATSAPP_VERIFY_TOKEN"): return int(challenge)
    return HTMLResponse(content="Verification token mismatch", status_code=403)

@app.post("/whatsapp-webhook")
async def receive_whatsapp_message(request: Request):
    try:
        body = await request.json()
        if body.get("object") == "whatsapp_business_account":
            for entry in body.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    if "messages" in value:
                        message = value["messages"][0]
                        sender_phone = message.get("from")
                        
                        incoming_text = ""
                        msg_type = message.get("type")
                        message_id = message.get("id")
                        
                        if msg_type == "text": incoming_text = message["text"]["body"]
                        elif msg_type == "interactive":
                            interactive_data = message.get("interactive", {})
                            if interactive_data.get("type") == "list_reply": incoming_text = interactive_data["list_reply"]["title"]
                            elif interactive_data.get("type") == "button_reply": incoming_text = interactive_data["button_reply"]["title"]
                        
                        if not incoming_text: continue
                            
                        if message_id:
                            is_duplicate = await redis_client.get(f"processed_msg:{message_id}")
                            if is_duplicate: return {"status": "success"}
                            await redis_client.setex(f"processed_msg:{message_id}", 60, "true")
                            
                        logger.info(f"💬 Incoming WhatsApp from {sender_phone}: {incoming_text}")

                        user_msg_lower = incoming_text.strip().lower()

                        if user_msg_lower == "reset":
                            await redis_client.delete(f"wa_history:{sender_phone}")
                            await redis_client.delete(f"last_doc_id:{sender_phone}")
                            clean_phone = sender_phone.replace("+91", "").replace("+", "")
                            await send_confirmation(clean_phone, "🧹 AI Memory wiped! We are starting completely fresh. Say 'Hi'!")
                            return {"status": "success"}
                        
                        elif user_msg_lower in ["hi", "hello", "hey", "start", "menu"]:
                            await redis_client.delete(f"wa_history:{sender_phone}")
                            await redis_client.delete(f"last_doc_id:{sender_phone}")
                            logger.info(f"🧹 Auto-wiped memory for {sender_phone} due to new greeting.")
                        
                        class WAParams:
                            def __init__(self): self.result = None
                            async def result_callback(self, result): self.result = result

                        async def wa_check_availability(problem_or_speciality: str, requested_date: str = None):
                            """
                            Check doctor availability by specialty.
                            Args:
                                problem_or_speciality: CRITICAL: You must map the user's symptoms to one of our exact official database titles: 'General Physician', 'Dermatologist', 'Cardiologist', 'Pediatrician', 'Orthopedic', or 'Urologist'. DO NOT use variations like 'Dermatology' or 'Pediatrics'.
                                requested_date: Optional. The specific date in YYYY-MM-DD format. DO NOT provide this if the user asks for "next available" or doesn't specify a day. Let the system find the next open slot automatically.
                            """
                            p = WAParams()
                            await check_availability(p, problem_or_speciality, requested_date)
                            result_data = p.result
                            if result_data.get("status") == "success" and result_data.get("doctors"):
                                doc = result_data["doctors"][0]
                                slots = doc.get("available_slots", [])
                                if slots:
                                    clean_phone = sender_phone.replace("+91", "").replace("+", "")
                                    await send_interactive_slots(clean_phone, doc["name"], doc["next_available_day"], slots)
                                    await redis_client.setex(f"last_doc_id:{sender_phone}", 86400, doc["id"])
                                    return {"status": "success", "message": "I have sent an interactive menu. Ask them to click it."}
                            return p.result

                        async def wa_book_appointment(doctor_id: str, patient_name: str, start_time_iso: str, phone: str, reason: str, force_book: bool = False, is_followup: str = "unknown"):
                            """
                            Book the appointment.
                            Args:
                                is_followup: "unknown" by default. Pass "yes" ONLY if the user explicitly confirmed it is a free 7-day follow up. Pass "no" if they confirmed it is a new issue.
                            """
                            p = WAParams()
                            start_time_iso = start_time_iso.replace("Z", "+05:30") if "Z" in start_time_iso else start_time_iso + "+05:30" if "+" not in start_time_iso else start_time_iso
                            
                            clean_phone_check = "".join(filter(str.isdigit, str(phone)))
                            if clean_phone_check.startswith("91") and len(clean_phone_check) == 12: clean_phone_check = clean_phone_check[2:]

                            chatting_phone = sender_phone.replace("+91", "").replace("+", "")

                            target_dt = datetime.fromisoformat(start_time_iso)
                            target_date_obj = target_dt.date()

                            try:
                                pool = await get_db_pool()
                                async with pool.acquire() as conn:
                                    if not force_book:
                                        upcoming_query = """
                                            SELECT a.appointment_start, d.name as doctor_name, d.speciality
                                            FROM appointments a
                                            JOIN patients p ON a.patient_id = p.id 
                                            JOIN doctors d ON a.doctor_id = d.id
                                            WHERE p.phone = $1 AND a.status IN ('confirmed', 'pending') AND a.appointment_start >= NOW()
                                            ORDER BY a.appointment_start ASC LIMIT 1
                                        """
                                        upcoming_appt = await conn.fetchrow(upcoming_query, clean_phone_check)
                                        if upcoming_appt:
                                            appt_time = upcoming_appt['appointment_start'].astimezone(pytz.timezone('Asia/Kolkata')).strftime('%b %d at %I:%M %p')
                                            doc_name = upcoming_appt['doctor_name']
                                            return {"status": "warning", "message": f"CRITICAL: Tell the user: 'Note: I see you already have an upcoming appointment on {appt_time} with {doc_name}. Do you want to proceed with booking an additional new appointment?'"}

                                    followup_query = """
                                        SELECT a.appointment_start, d.name as doctor_name, p.name as patient_name 
                                        FROM appointments a
                                        JOIN patients p ON a.patient_id = p.id 
                                        JOIN doctors d ON a.doctor_id = d.id
                                        WHERE p.phone = $1 AND a.status = 'confirmed' 
                                          AND a.appointment_start >= NOW() - INTERVAL '7 days' 
                                          AND a.appointment_start < NOW()
                                        ORDER BY a.appointment_start DESC LIMIT 1
                                    """
                                    has_recent = await conn.fetchrow(followup_query, clean_phone_check)

                                    if is_followup == "unknown":
                                        if has_recent: 
                                            recent_date = has_recent['appointment_start'].astimezone(pytz.timezone('Asia/Kolkata')).strftime('%B %d')
                                            recent_patient = has_recent['patient_name']
                                            recent_doc = has_recent['doctor_name']
                                            
                                            return {"status": "warning", "message": f"CRITICAL: Tell the user: 'I see {recent_patient} had a confirmed appointment with {recent_doc} on {recent_date}. Is this a free 1-week follow-up for that visit, or a completely new medical problem?'"}
                                        else: 
                                            is_followup_bool = False
                                    elif is_followup == "yes":
                                        if has_recent: is_followup_bool = True
                                        else: return {"status": "warning", "message": "CRITICAL: Tell the user: 'Your free 1-week follow-up period has expired, or no previous record was found. I will need to book this as a new paid consultation. Shall I proceed?'"}
                                    else:
                                        is_followup_bool = False

                            except Exception as e: logger.warning(f"⚠️ DB Error: {e}")

                            real_doc_id = await redis_client.get(f"last_doc_id:{sender_phone}")
                            if real_doc_id: doctor_id = real_doc_id

                            await book_appointment(p, doctor_id, patient_name, start_time_iso, phone, reason, force_book, is_followup_bool, chatting_phone=chatting_phone)
                            
                            result_data = p.result
                            if result_data and result_data.get("status") == "success":
                                if not result_data.get("is_followup"): 
                                    appt_id = result_data.get("appointment_id")
                                    if appt_id: 
                                        asyncio.create_task(cancel_unpaid_appointment(appt_id))
                                        # 🟢 NEW: Cache the chatting phone so Razorpay webhook can message it later!
                                        if redis_client:
                                            await redis_client.setex(f"appt_chat:{appt_id}", 86400, chatting_phone)
                                            
                                    return {"status": "success", "message": "Say EXACTLY: 'Your appointment has been tentatively booked! Please click the Razorpay link above to confirm your slot. Note: The payment link expires in 15 minutes.'"}
                                else:
                                    return {"status": "success", "message": "Say EXACTLY: 'Your free 1-week follow-up has been successfully booked! No payment is required. See you then!'"}
                            
                            return result_data
                            
                        async def wa_lookup_appointment(phone: str):
                            p = WAParams()
                            await lookup_appointment(p, phone)
                            return p.result

                        whatsapp_tools = [wa_check_availability, wa_book_appointment, wa_lookup_appointment]
                        
                        history_key = f"wa_history:{sender_phone}"
                        chat_history_str = await redis_client.get(history_key)
                        chat_history = []
                        
                        if chat_history_str:
                            for msg in json.loads(chat_history_str):
                                valid_parts = [types.Part.from_text(text=p) for p in msg.get("parts", []) if p]
                                if valid_parts: chat_history.append(types.Content(role=msg["role"], parts=valid_parts))

                        chat_history.append(types.Content(role="user", parts=[types.Part.from_text(text=incoming_text)]))

                        ai_reply = ""
                        while True:
                            response = await gemini_client.aio.models.generate_content(
                                model='gemini-2.5-flash',
                                contents=chat_history,
                                config=types.GenerateContentConfig(system_instruction=WHATSAPP_SYSTEM_PROMPT, tools=whatsapp_tools)
                            )
                            
                            if response.text:
                                ai_reply = response.text
                                break
                            
                            if response.function_calls:
                                chat_history.append(response.candidates[0].content)
                                for function_call in response.function_calls:
                                    func_name = function_call.name
                                    func_args = function_call.args
                                    logger.info(f"🛠️ LLM called: {func_name} | args: {func_args}")
                                    
                                    tool_map = {f.__name__: f for f in whatsapp_tools}
                                    if func_name in tool_map:
                                        result = await tool_map[func_name](**func_args) if asyncio.iscoroutinefunction(tool_map[func_name]) else tool_map[func_name](**func_args)
                                        tool_result_part = types.Part.from_function_response(name=func_name, response={"result": result})
                                        chat_history.append(types.Content(role="user", parts=[tool_result_part]))
                        
                        storable_history = []
                        for c in chat_history:
                            text_parts = [p.text for p in c.parts if p.text]
                            if text_parts: storable_history.append({"role": c.role, "parts": text_parts})
                                
                        storable_history.append({"role": "model", "parts": [ai_reply]})
                        await redis_client.setex(history_key, 86400, json.dumps(storable_history))

                        clean_phone = sender_phone.replace("+91", "").replace("+", "")
                        await send_confirmation(clean_phone, ai_reply)
                            
        return {"status": "success"}
    except Exception as e:
        logger.error(f"❌ WhatsApp Webhook Error: {e}")
        return {"status": "error"}

if __name__ == "__main__":
    logger.info("🚀 Starting Standalone WhatsApp Webhook Server on Port 8000...")
    uvicorn.run("whatsapp_agent:app", host="0.0.0.0", port=8000, reload=True)