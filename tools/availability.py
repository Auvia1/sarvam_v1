#tools/availability.py
import pytz
import datetime
from dateutil import parser as date_parser
from loguru import logger
from pipecat.services.llm_service import FunctionCallParams
from db.queries import find_available_doctors, get_clinic_id, get_doctor_booked_slots
from tools.pool import get_pool

async def check_availability(params: FunctionCallParams, problem_or_speciality: str, requested_date: str = None):
    logger.info(f"🔍 Tool Call: check_availability for '{problem_or_speciality}' | Date: {requested_date}")
    
    pool = get_pool()
    clinic_id = await get_clinic_id(pool)
    records = await find_available_doctors(pool, clinic_id, problem_or_speciality)
    
    if not records:
        error_msg = f"No doctors found for '{problem_or_speciality}'. Tell the user this specialty is unavailable and explicitly ask: 'Shall I check if a General Physician is available?'. STOP SPEAKING IMMEDIATELY. DO NOT invent dates or times. YOU MUST WAIT for the user to say yes."
        logger.warning(error_msg)
        await params.result_callback({"status": "error", "message": error_msg})
        return

    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.datetime.now(ist)
    
    target_date = None
    if requested_date:
        try:
            parsed_dt = date_parser.parse(requested_date)
            if parsed_dt.tzinfo is None:
                parsed_dt = ist.localize(parsed_dt)
            target_date = parsed_dt.astimezone(ist).date()
            logger.info(f"📅 Parsed Requested Date successfully as: {target_date}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to parse requested_date '{requested_date}': {e}")

    doctors_map = {}
    for r in records:
        doc_id = str(r['id'])
        if doc_id not in doctors_map:
            doctors_map[doc_id] = {"id": doc_id, "name": f"{r['name']}, {r['speciality']}", "schedules": {}}
        doctors_map[doc_id]["schedules"][r['day_of_week']] = (r['start_time'], r['end_time'])

    doctors_result = []
    for doc_id, doc_data in doctors_map.items():
        days_checked = 0
        while days_checked < 14:
            check_dt = now + datetime.timedelta(days=days_checked)
            check_date_obj = check_dt.date()
            
            if target_date and check_date_obj != target_date:
                days_checked += 1
                continue

            pg_dow = (check_date_obj.weekday() + 1) % 7 
            
            # 🟢 REASONING LOG: Check if doctor works today
            if pg_dow in doc_data["schedules"]:
                start_time, end_time = doc_data["schedules"][pg_dow]
                booked_times = await get_doctor_booked_slots(pool, doc_id, check_date_obj)
                
                available_slots = []
                current_dt = datetime.datetime.combine(check_date_obj, start_time)
                end_dt = datetime.datetime.combine(check_date_obj, end_time)
                lunch_start, lunch_end = datetime.time(12, 0), datetime.time(13, 0)

                while current_dt < end_dt:
                    t = current_dt.time()
                    if not (t >= lunch_start and t < lunch_end):
                        time_formatted = current_dt.strftime("%I:%M %p")
                        if time_formatted not in booked_times:
                            if (check_date_obj == now.date()) and current_dt.time() <= now.time():
                                pass # Past time, ignore
                            else:
                                available_slots.append(time_formatted)
                    current_dt += datetime.timedelta(minutes=30)
                
                if available_slots:
                    logger.info(f"✅ Found {len(available_slots)} free slots on {check_dt.strftime('%A, %b %d')}")
                    doctors_result.append({
                        "id": doc_id,
                        "name": doc_data["name"],
                        "is_available_today": (check_date_obj == now.date()),
                        "days_until_available": days_checked,
                        "next_available_day": check_dt.strftime('%A, %B %d'),
                        "working_hours": f"{start_time.strftime('%I:%M %p')} to {end_time.strftime('%I:%M %p')}",
                        "lunch_break": "12:00 PM to 01:00 PM",
                        "first_available_slot": available_slots[0],
                        "available_slots": available_slots 
                    })
                    break 
                else:
                    # 🟢 REASONING LOG: Why no slots?
                    logger.info(f"⏭️ Skipping {check_dt.strftime('%A, %b %d')} - All slots are either booked or in the past.")
            else:
                # 🟢 REASONING LOG: Doctor doesn't work this day
                logger.info(f"⏭️ Skipping {check_dt.strftime('%A, %b %d')} - Doctor is not scheduled to work on Day Index {pg_dow}.")
                
            days_checked += 1

    if not doctors_result:
        error_msg = "No available slots found for the requested date or specialty. Tell the user nothing is available."
        await params.result_callback({"status": "error", "message": error_msg})
        return

    await params.result_callback({"status": "success", "doctors": doctors_result})