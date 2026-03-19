import os
import datetime
from loguru import logger
import redis.asyncio as redis
from pipecat.services.llm_service import FunctionCallParams

from db.connection import get_db_pool
from db.queries import get_or_create_patient, book_new_appointment, lookup_active_appointment
from tools.pool import get_pool
from tools.payment import generate_payment_link
from tools.notify import send_confirmation

async def book_appointment(params: FunctionCallParams, doctor_id: str, patient_name: str, start_time_iso: str, phone: str, force_book: bool = False):
    clean_name = patient_name.strip()
    clean_phone = "".join(filter(str.isdigit, str(phone)))
    if clean_phone.startswith("91") and len(clean_phone) == 12:
        clean_phone = clean_phone[2:]

    logger.info(f"📅 Tool Call: book_appointment | Name: {clean_name} | Phone: {clean_phone} | Force: {force_book}")

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    redis_client = redis.from_url(redis_url, decode_responses=True)
    lock_key = f"booking_lock:doctor_{doctor_id}:time_{start_time_iso}:phone_{clean_phone}"
    
    try:
        lock_acquired = await redis_client.set(lock_key, "locked", nx=True, ex=10)

        if not lock_acquired:
            logger.warning(f"🚨 Double-booking prevented! Slot {start_time_iso} is actively being booked by someone else.")
            await params.result_callback({
                "status": "error",
                "message": "CRITICAL: Another patient is booking this EXACT slot right now! Say: 'I am so sorry, but someone literally just grabbed that slot a second ago. Should we look at the next available time?'"
            })
            return

        pool = await get_db_pool()
        async with pool.acquire() as conn:
            # 1. Fetch clinic_id from doctors table safely
            try:
                clinic_id_query = "SELECT clinic_id FROM doctors WHERE id = $1::uuid"
                clinic_id = await conn.fetchval(clinic_id_query, doctor_id)
            except Exception as e:
                logger.error(f"❌ Invalid UUID format or DB error: {e}")
                clinic_id = None

            # 2. Catch Hallucinations or Invalid IDs
            if not clinic_id:
                logger.error(f"❌ Doctor ID {doctor_id} not found in DB! Gemini hallucinated.")
                await params.result_callback({
                    "status": "error", 
                    "message": "CRITICAL ERROR: The doctor_id you provided does not exist. You hallucinated or used an old ID. Look at the MOST RECENT 'check_availability' tool response and use that exact 36-character 'id'. Call book_appointment again immediately with the correct ID."
                })
                return

            # 3. Handle Family Bookings correctly with exact name + phone match
            patient_id = await get_or_create_patient(conn, str(clinic_id), clean_name, clean_phone)

        start_dt = datetime.datetime.fromisoformat(start_time_iso)
        end_dt = start_dt + datetime.timedelta(minutes=30)
        
        appt_id = await book_new_appointment(
            pool=pool, clinic_id=clinic_id, doctor_id=doctor_id, 
            patient_name=clean_name, phone=clean_phone, start_time=start_dt,
            end_time=end_dt, force_book=force_book, patient_id=patient_id
        )
        
        # --- WARNING LOGIC WITH DOCTOR DETAILS ---
        if str(appt_id).startswith("HAS_OTHER_APPOINTMENT"):
            parts = appt_id.split("|")
            existing_time = parts[2]
            existing_status = parts[3]
            doc_name = parts[4]
            doc_spec = parts[5]
            
            if existing_status == "pending":
                msg = f"User has an UNPAID appointment at {existing_time} with {doc_name} ({doc_spec}). Ask them exactly: 'You already have an unpaid appointment at {existing_time} with {doc_name}, {doc_spec}. Should I resend the payment link for that, or do you want to book a totally new appointment?'"
            else:
                msg = f"User already has a PAID/CONFIRMED appointment at {existing_time} with {doc_name} ({doc_spec}). Ask them: 'You already have a confirmed, paid appointment at {existing_time} with {doc_name}, {doc_spec}. Are you sure you want to book another new one?'"
                
            await params.result_callback({"status": "warning", "message": msg})
            return

        elif appt_id == "ALREADY_BOOKED_BY_USER":
            await params.result_callback({"status": "error", "message": "User already has an appointment booked for this exact time."})
            return

        elif appt_id == "SLOT_TAKEN":
            await params.result_callback({"status": "error", "message": "This slot was just taken by someone else."})
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
            await send_confirmation(clean_phone, whatsapp_msg)

        logger.info(f"✅ Appointment booked successfully: {appt_id}")
        await params.result_callback({"status": "success", "appointment_id": str(appt_id)})
        
    except Exception as e:
        logger.error(f"❌ Exception during booking: {e}")
        await params.result_callback({"status": "error", "message": f"System error: {str(e)}"})

    finally:
        await redis_client.close()


async def resend_payment_link(params: FunctionCallParams, phone: str):
    """Fetches the latest pending/confirmed appointment and resends the payment link."""
    logger.info(f"🔄 Tool Call: resend_payment_link | Phone: {phone}")
    try:
        pool = get_pool()
        appt = await lookup_active_appointment(pool, phone)
        
        if not appt:
            await params.result_callback({"status": "error", "message": "No active or pending appointments found for this phone number."})
            return
            
        appt_id = appt["id"]
        patient_name = appt["patient_name"]
        
        consultation_fee = 500
        payment_link = await generate_payment_link(consultation_fee, phone, str(appt_id), patient_name)
        
        if payment_link:
            whatsapp_msg = (
                f"🏥 *Mithra Hospitals*\n\n"
                f"Hi {patient_name}, here is your requested payment link: {payment_link}\n\n"
                f"Please pay ₹{consultation_fee} to confirm your slot."
            )
            await send_confirmation(phone, whatsapp_msg)
            await params.result_callback({"status": "success", "message": "Payment link resent successfully. Tell the user you have sent it!"})
        else:
            await params.result_callback({"status": "error", "message": "Failed to generate payment link."})
            
    except Exception as e:
        logger.error(f"❌ Exception during resend: {e}")
        await params.result_callback({"status": "error", "message": f"System error: {str(e)}"})


async def mark_appointment_paid(appointment_id: str):
    """Updates the database when Razorpay confirms a successful payment."""
    pool = get_pool()
    query = """
        UPDATE appointments
        SET status = 'confirmed', payment_status = 'paid', updated_at = NOW()
        WHERE id = $1::uuid
    """

    try:
        async with pool.acquire() as conn:
            await conn.execute(query, appointment_id)
            logger.info(f"✅ Database updated: Appointment {appointment_id} marked as PAID and CONFIRMED.")
    except Exception as e:
        logger.error(f"❌ Failed to mark appointment as paid in DB: {e}")