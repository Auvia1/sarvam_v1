from pipecat.services.llm_service import FunctionCallParams
from db.queries import lookup_active_appointment
from tools.pool import get_pool
from datetime import datetime, timedelta

async def lookup_appointment(params: FunctionCallParams, phone: str):
    """Look up an existing appointment using the patient's phone number.
    Args:
        phone: 10-digit phone number.
    """
    pool = get_pool()
    record = await lookup_active_appointment(pool, phone)
    
    if not record:
        await params.result_callback({"status": "not_found"})
        return
        
    await params.result_callback({
        "status": "found", 
        "appointment_id": str(record['id']),
        "patient": record['patient_name'],
        "doctor": record['doctor_name'],
        "time": str(record['appointment_start'])
    })

async def reschedule_appointment(params: FunctionCallParams, appointment_id: str, new_start_time_iso: str):
    """Reschedule an existing appointment to a new time.
    Args:
        appointment_id: The UUID of the appointment.
        new_start_time_iso: The new time in ISO 8601 format.
    """
    pool = get_pool()
    start_time = datetime.fromisoformat(new_start_time_iso)
    end_time = start_time + timedelta(minutes=30)
    
    await pool.execute("UPDATE appointments SET appointment_start=$1, appointment_end=$2, status='rescheduled' WHERE id=$3", start_time, end_time, appointment_id)
    await params.result_callback({"status": "success"})