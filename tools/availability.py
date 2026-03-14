from pipecat.services.llm_service import FunctionCallParams
from db.queries import find_available_doctors, get_clinic_id, get_doctor_booked_slots
from tools.pool import get_pool
from loguru import logger
import datetime
import pytz

async def check_availability(params: FunctionCallParams, problem_or_speciality: str):
    logger.info(f"🔍 Tool Call: check_availability for '{problem_or_speciality}'")
    
    pool = get_pool()
    clinic_id = await get_clinic_id(pool)
    records = await find_available_doctors(pool, clinic_id, problem_or_speciality)
    
    if not records:
        # --- THE HALLUCINATION FIX ---
        error_msg = f"No doctors found for '{problem_or_speciality}'. Tell the user this specialty is unavailable and explicitly ask: 'Shall I check if a General Physician is available?'. STOP SPEAKING IMMEDIATELY. DO NOT invent dates or times. YOU MUST WAIT for the user to say yes."
        logger.warning(error_msg)
        await params.result_callback({"status": "error", "message": error_msg})
        return

    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.datetime.now(ist)
    
    # 1. Group the schedule rows by doctor
    doctors_map = {}
    for r in records:
        doc_id = str(r['id'])
        if doc_id not in doctors_map:
            doctors_map[doc_id] = {
                "id": doc_id,
                "name": f"{r['name']}, {r['speciality']}",
                "schedules": {}
            }
        doctors_map[doc_id]["schedules"][r['day_of_week']] = (r['start_time'], r['end_time'])

    # 2. Calculate exact available slots
    doctors_result = []
    for doc_id, doc_data in doctors_map.items():
        days_checked = 0
        
        while days_checked < 14: # Look ahead up to 2 weeks
            check_date = now + datetime.timedelta(days=days_checked)
            pg_dow = (check_date.weekday() + 1) % 7 
            
            # --- NEW LOG TO SHOW DAY CALCULATION ---
            logger.debug(f"📅 Checking Date: {check_date.date()} (DB Day Index: {pg_dow}) for {doc_data['name']}")
            
            if pg_dow in doc_data["schedules"]:
                logger.info(f"✅ Schedule match found on {check_date.strftime('%A, %B %d')} for {doc_data['name']}!")
                start_time, end_time = doc_data["schedules"][pg_dow]
                
                # Fetch existing bookings from the DB
                booked_times = await get_doctor_booked_slots(pool, doc_id, check_date.date())
                logger.info(f"🔒 Booked slots from DB: {booked_times}")
                
                # Generate 30-minute slots
                available_slots = []
                current_dt = datetime.datetime.combine(check_date, start_time)
                end_dt = datetime.datetime.combine(check_date, end_time)
                lunch_start = datetime.time(12, 0)
                lunch_end = datetime.time(13, 0)

                while current_dt < end_dt:
                    t = current_dt.time()
                    if not (t >= lunch_start and t < lunch_end):
                        time_formatted = current_dt.strftime("%I:%M %p")
                        
                        if time_formatted not in booked_times:
                            if days_checked == 0 and current_dt.time() <= now.time():
                                pass
                            else:
                                available_slots.append(time_formatted)
                    
                    current_dt += datetime.timedelta(minutes=30)
                
                if available_slots:
                    logger.info(f"🎉 Free slots generated: {available_slots}")
                    is_available_today = days_checked == 0
                    doctors_result.append({
                        "id": doc_id,
                        "name": doc_data["name"],
                        "is_available_today": is_available_today,
                        "days_until_available": days_checked,
                        "next_available_day": check_date.strftime('%A, %B %d'),
                        "working_hours": f"{start_time.strftime('%I:%M %p')} to {end_time.strftime('%I:%M %p')}",
                        "lunch_break": "12:00 PM to 01:00 PM",
                        "first_available_slot": available_slots[0],
                        "available_slots": available_slots 
                    })
                    break
                else:
                    logger.info(f"⚠️ No free slots left on {check_date.strftime('%A, %B %d')}. Checking next day...")
                
            days_checked += 1

    if not doctors_result:
        error_msg = "No available slots found for the requested specialty in the next 14 days. Tell the user no slots are available and ask them to try another specialty or date."
        logger.warning(error_msg)
        await params.result_callback({"status": "error", "message": error_msg})
        return

    logger.info(f"📤 Returning exact schedules to LLM: {doctors_result}")
    await params.result_callback({"status": "success", "doctors": doctors_result})