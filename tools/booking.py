import datetime
from loguru import logger
from pipecat.services.llm_service import FunctionCallParams
from db.queries import get_clinic_id, book_new_appointment
from tools.pipecat_tools import get_pool

# Add force_book: bool = False to the function arguments!
async def book_appointment(params: FunctionCallParams, doctor_id: str, patient_name: str, start_time_iso: str, phone: str, force_book: bool = False):
    logger.info(f"📅 Tool Call: book_appointment | Name: {patient_name} | Phone: {phone} | Force: {force_book}")
    
    try:
        pool = get_pool()
        clinic_id = await get_clinic_id(pool)
        start_dt = datetime.datetime.fromisoformat(start_time_iso)
        end_dt = start_dt + datetime.timedelta(minutes=30)
        
        # Pass force_book down to the database function
        appt_id = await book_new_appointment(
            pool=pool, clinic_id=clinic_id, doctor_id=doctor_id, 
            patient_name=patient_name, phone=phone, start_time=start_dt, 
            end_time=end_dt, force_book=force_book
        )
        
        # NEW: Handle the existing appointment warning!
        if str(appt_id).startswith("HAS_OTHER_APPOINTMENT"):
            _, existing_time, existing_status = appt_id.split("|")
            msg = f"User already has a '{existing_status}' appointment at {existing_time}. Ask them: 'You already have an appointment booked at {existing_time}. Are you sure you want to book a new one?' If yes, call this tool again with force_book=true."
            logger.warning(f"⚠️ User has existing appointment at {existing_time}. Warning the AI.")
            await params.result_callback({"status": "warning", "message": msg})
            return
            
        elif appt_id == "ALREADY_BOOKED_BY_USER":
            await params.result_callback({"status": "error", "message": "User already has an appointment booked for this exact time. Tell them!"})
            return
            
        elif appt_id == "SLOT_TAKEN":
            await params.result_callback({"status": "error", "message": "This slot was just taken by someone else. Ask them to pick a new time."})
            return

        # If successful:
        logger.info(f"✅ Appointment booked successfully: {appt_id}")
        await params.result_callback({"status": "success", "appointment_id": str(appt_id)})
        
    except Exception as e:
        logger.error(f"❌ DB Exception during booking: {e}")
        await params.result_callback({"status": "error", "message": f"Database error: {str(e)}"})