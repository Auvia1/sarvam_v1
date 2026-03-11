from pipecat.services.llm_service import FunctionCallParams
from db.queries import find_available_doctors, get_clinic_id, get_doctor_booked_slots
from tools.pipecat_tools import get_pool
from loguru import logger
import datetime
import pytz

async def check_availability(params: FunctionCallParams, problem_or_speciality: str):
    logger.info(f"🔍 Tool Call: check_availability for '{problem_or_speciality}'")
    
    pool = get_pool()
    clinic_id = await get_clinic_id(pool)
    records = await find_available_doctors(pool, clinic_id, problem_or_speciality)
    
    if not records:
        await params.result_callback({"status": "no doctors found. Suggest a general physician."})
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
            
            if pg_dow in doc_data["schedules"]:
                start_time, end_time = doc_data["schedules"][pg_dow]
                
                # Fetch existing bookings from the DB
                booked_times = await get_doctor_booked_slots(pool, doc_id, check_date.date())
                
                # Generate 30-minute slots
                available_slots = []
                current_dt = datetime.datetime.combine(check_date, start_time)
                end_dt = datetime.datetime.combine(check_date, end_time)
                lunch_start = datetime.time(12, 0)
                lunch_end = datetime.time(13, 0)

                while current_dt < end_dt:
                    t = current_dt.time()
                    # Skip Lunch Break
                    if not (t >= lunch_start and t < lunch_end):
                        time_formatted = current_dt.strftime("%I:%M %p")
                        
                        # Skip if Booked
                        if time_formatted not in booked_times:
                            # Skip if the time has already passed today
                            if days_checked == 0 and current_dt.time() <= now.time():
                                pass
                            else:
                                available_slots.append(time_formatted)
                    
                    current_dt += datetime.timedelta(minutes=30)
                
                # If we found at least one free slot on this day, stop searching
                if available_slots:
                    doctors_result.append({
                        "id": doc_id,
                        "name": doc_data["name"],
                        "next_available_day": check_date.strftime('%A, %B %d'),
                        "working_hours": f"{start_time.strftime('%I:%M %p')} to {end_time.strftime('%I:%M %p')}",
                        "lunch_break": "12:00 PM to 01:00 PM",
                        "first_available_slot": available_slots[0],  # <--- THE FIX: Explicitly handing it the nearest slot
                        "available_slots": available_slots 
                    })
                    break
                
            days_checked += 1

    logger.info(f"✅ Returning exact schedules & free slots to LLM: {doctors_result}")
    await params.result_callback({"status": "success", "doctors": doctors_result})