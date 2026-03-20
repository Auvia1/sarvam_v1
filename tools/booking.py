#tools/booking.py
import os
import datetime
import asyncio
from loguru import logger
import redis.asyncio as redis
from pipecat.services.llm_service import FunctionCallParams

from db.connection import get_db_pool
from db.queries import get_or_create_patient, book_new_appointment, lookup_active_appointment
from tools.pool import get_pool
from tools.payment import generate_payment_link
from tools.notify import send_confirmation

# 🟢 Added 'chatting_phone' to the parameters
async def book_appointment(params: FunctionCallParams, doctor_id: str, patient_name: str, start_time_iso: str, phone: str, reason: str, force_book: bool = False, is_followup: bool = False, chatting_phone: str = None):
    clean_name = patient_name.strip()
    clean_phone = "".join(filter(str.isdigit, str(phone)))
    if clean_phone.startswith("91") and len(clean_phone) == 12:
        clean_phone = clean_phone[2:]

    # 🟢 Clean the chatting phone so we can send links to it
    if chatting_phone:
        clean_chatting = "".join(filter(str.isdigit, str(chatting_phone)))
        if clean_chatting.startswith("91") and len(clean_chatting) == 12:
            clean_chatting = clean_chatting[2:]
    else:
        clean_chatting = clean_phone

    logger.info(f"📅 Tool Call: book_appointment | Name: {clean_name} | Patient Phone: {clean_phone} | Chat Phone: {clean_chatting}")

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    redis_client = redis.from_url(redis_url, decode_responses=True)
    lock_key = f"booking_lock:doctor_{doctor_id}:time_{start_time_iso}:phone_{clean_phone}"
    
    try:
        lock_acquired = await redis_client.set(lock_key, "locked", nx=True, ex=10)
        if not lock_acquired:
            await params.result_callback({"status": "error", "message": "CRITICAL: Another patient just grabbed that slot! Ask to book another."})
            return

        pool = await get_db_pool()
        async with pool.acquire() as conn:
            clinic_id_query = "SELECT clinic_id FROM doctors WHERE id = $1::uuid"
            clinic_id = await conn.fetchval(clinic_id_query, doctor_id)
            patient_id = await get_or_create_patient(conn, str(clinic_id), clean_name, clean_phone)

        start_dt = datetime.datetime.fromisoformat(start_time_iso)
        end_dt = start_dt + datetime.timedelta(minutes=30)
        
        appt_id = await book_new_appointment(
            pool=pool, clinic_id=clinic_id, doctor_id=doctor_id, 
            patient_name=clean_name, phone=clean_phone, start_time=start_dt,
            end_time=end_dt, force_book=force_book, patient_id=patient_id,
            reason=reason, is_followup=is_followup
        )
        
        if str(appt_id).startswith("HAS_OTHER_APPOINTMENT") or appt_id in ["ALREADY_BOOKED_BY_USER", "SLOT_TAKEN"]:
            await params.result_callback({"status": "error", "message": "Slot error."})
            return

        # 🟢 Create a unique set of phone numbers to notify (removes duplicates if they are the same)
        target_phones = {p for p in [clean_phone, clean_chatting] if p}

        if is_followup:
            whatsapp_msg = (
                f"🏥 *Mithra Hospitals*\n\n"
                f"Hi {clean_name}, your free 1-week follow-up is CONFIRMED for "
                f"{start_dt.strftime('%I:%M %p')} on {start_dt.strftime('%B %d')}.\n\n"
                f"No payment is required. See you then!"
            )
            # 🟢 Send to both numbers
            for target in target_phones:
                await send_confirmation(target, whatsapp_msg)
                
            logger.info(f"✅ Free Follow-up booked: {appt_id}")
            await params.result_callback({"status": "success", "appointment_id": str(appt_id), "is_followup": True})
            return

        consultation_fee = 500
        payment_link = await generate_payment_link(consultation_fee, clean_phone, str(appt_id), clean_name)

        if payment_link:
            whatsapp_msg = (
                f"🏥 *Mithra Hospitals*\n\n"
                f"Hi {clean_name}, your appointment is tentatively booked for "
                f"{start_dt.strftime('%I:%M %p')} on {start_dt.strftime('%B %d')}.\n\n"
                f"Please pay ₹{consultation_fee} using this link to confirm your slot "
                f"(Valid for 15 mins): {payment_link}"
            )
            # 🟢 Send to both numbers
            for target in target_phones:
                await send_confirmation(target, whatsapp_msg)

        logger.info(f"✅ Standard Appointment booked: {appt_id}")
        await params.result_callback({"status": "success", "appointment_id": str(appt_id), "is_followup": False})
        
    except Exception as e:
        logger.error(f"❌ Exception during booking: {e}")
        await params.result_callback({"status": "error", "message": f"System error: {str(e)}"})
    finally:
        await redis_client.close()

async def mark_appointment_paid(appointment_id: str):
    pool = get_pool()
    query = "UPDATE appointments SET status = 'confirmed', payment_status = 'paid', updated_at = NOW() WHERE id = $1::uuid"
    try:
        async with pool.acquire() as conn:
            await conn.execute(query, appointment_id)
    except Exception as e:
        logger.error(f"❌ Failed to mark appointment as paid in DB: {e}")

async def cancel_unpaid_appointment(appointment_id: str):
    logger.info(f"⏳ Timer started: Checking if {appointment_id} is paid in 15 minutes...")
    await asyncio.sleep(900)
    pool = get_pool()
    query = "UPDATE appointments SET status = 'cancelled', updated_at = NOW() WHERE id = $1::uuid AND status = 'pending'"
    async with pool.acquire() as conn:
        await conn.execute(query, appointment_id)