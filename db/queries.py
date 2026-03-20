#db/queries.py
from loguru import logger

async def get_or_create_patient(conn, clinic_id: str, patient_name: str, phone: str) -> str:
    clean_name = patient_name.strip()
    clean_phone = phone.strip()
    
    find_query = "SELECT id FROM patients WHERE clinic_id = $1 AND phone = $2 AND LOWER(name) = LOWER($3)"
    row = await conn.fetchrow(find_query, clinic_id, clean_phone, clean_name)
    
    if row:
        logger.info(f"👤 Found existing patient: {clean_name} ({clean_phone})")
        return str(row['id'])
        
    insert_query = "INSERT INTO patients (clinic_id, name, phone) VALUES ($1, $2, $3) RETURNING id"
    new_id = await conn.fetchval(insert_query, clinic_id, clean_name, clean_phone)
    logger.info(f"🆕 Created NEW patient profile: {clean_name} ({clean_phone})")
    return str(new_id)

async def get_clinic_id(pool):
    return await pool.fetchval("SELECT id FROM clinics WHERE name = 'Mithra Hospital' LIMIT 1")

async def find_available_doctors(pool, clinic_id, specialty_keyword):
    query = """
        SELECT d.id, d.name, d.speciality, ds.day_of_week, ds.start_time, ds.end_time
        FROM doctors d
        JOIN doctor_schedule ds ON d.id = ds.doctor_id
        WHERE d.clinic_id = $1 
          AND d.speciality ILIKE $2
          AND ds.effective_from <= CURRENT_DATE
          AND (ds.effective_to IS NULL OR ds.effective_to >= CURRENT_DATE)
    """
    async with pool.acquire() as conn:
        records = await conn.fetch(query, clinic_id, f"%{specialty_keyword}%")
        logger.info(f"🏥 DB FETCH: Searched for '{specialty_keyword}'. Found {len(records)} schedule rows.")
        return records

async def lookup_active_appointment(pool, phone):
    query = """
        SELECT a.id, p.name as patient_name, d.name as doctor_name, a.appointment_start
        FROM appointments a
        JOIN patients p ON a.patient_id = p.id
        JOIN doctors d ON a.doctor_id = d.id
        WHERE p.phone = $1 
          AND a.appointment_start > NOW()
          AND (a.status = 'confirmed' OR (a.status = 'pending' AND a.created_at >= NOW() - INTERVAL '15 minutes'))
        ORDER BY a.appointment_start ASC
    """
    return await pool.fetch(query, phone)

async def get_doctor_booked_slots(pool, doctor_id: str, date_obj):
    query = """
        SELECT TO_CHAR(appointment_start AT TIME ZONE 'Asia/Kolkata', 'HH12:MI AM') as time_str
        FROM appointments 
        WHERE doctor_id = $1::uuid 
          AND DATE(appointment_start AT TIME ZONE 'Asia/Kolkata') = $2
          AND (status = 'confirmed' OR (status = 'pending' AND created_at >= NOW() - INTERVAL '15 minutes'))
    """
    try:
        async with pool.acquire() as conn:
            records = await conn.fetch(query, doctor_id, date_obj)
            return [r['time_str'] for r in records]
    except Exception as e:
        return []

# 🟢 Updated to accept 'is_followup'
async def book_new_appointment(pool, clinic_id, doctor_id, patient_name, phone, start_time, end_time, force_book=False, patient_id=None, reason=None, is_followup=False):
    async with pool.acquire() as conn:
        cleanup_query = "UPDATE appointments SET status = 'cancelled', updated_at = NOW() WHERE status = 'pending' AND created_at < NOW() - INTERVAL '15 minutes'"
        await conn.execute(cleanup_query)

        resolved_patient_id = patient_id or await get_or_create_patient(conn, clinic_id, patient_name, phone)

        check_query = """
            SELECT patient_id FROM appointments
            WHERE doctor_id = $1 AND appointment_start = $2
            AND (status = 'confirmed' OR (status = 'pending' AND created_at >= NOW() - INTERVAL '15 minutes'))
        """
        existing_patient = await conn.fetchval(check_query, doctor_id, start_time)

        if existing_patient:
            if str(existing_patient) == str(resolved_patient_id):
                return "ALREADY_BOOKED_BY_USER"
            else:
                return "SLOT_TAKEN"

        # 🟢 If follow-up, mark as confirmed and free! Otherwise, pending and paid.
        status = 'confirmed' if is_followup else 'pending'
        payment_status = 'free_followup' if is_followup else 'unpaid'
        payment_amount = 0.00 if is_followup else 500.00

        insert_query = """
            INSERT INTO appointments (clinic_id, patient_id, doctor_id, appointment_start, appointment_end, status, reason, payment_status, payment_amount)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (doctor_id, appointment_start) 
            DO UPDATE SET 
                patient_id = EXCLUDED.patient_id,
                appointment_end = EXCLUDED.appointment_end,
                status = EXCLUDED.status,
                reason = EXCLUDED.reason,
                payment_status = EXCLUDED.payment_status,
                payment_amount = EXCLUDED.payment_amount,
                created_at = NOW()
            RETURNING id
        """
        appt_id = await conn.fetchval(insert_query, clinic_id, resolved_patient_id, doctor_id, start_time, end_time, status, reason, payment_status, payment_amount)
        return appt_id