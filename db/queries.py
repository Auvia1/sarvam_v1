async def get_clinic_id(pool):
    # Hardcoded for demo, but normally fetched via API key/tenant ID
    return await pool.fetchval("SELECT id FROM clinics WHERE name = 'Mithra Hospital' LIMIT 1")

async def find_available_doctors(pool, clinic_id, specialty_keyword):
    """Fetch doctors matching the specialty along with their precise weekly schedule."""
    query = """
        SELECT 
            d.id, 
            d.name, 
            d.speciality,
            ds.day_of_week,
            ds.start_time,
            ds.end_time
        FROM doctors d
        JOIN doctor_schedule ds ON d.id = ds.doctor_id
        WHERE d.clinic_id = $1 
          AND d.speciality ILIKE $2
          AND ds.effective_from <= CURRENT_DATE
          AND (ds.effective_to IS NULL OR ds.effective_to >= CURRENT_DATE)
    """
    async with pool.acquire() as conn:
        return await conn.fetch(query, clinic_id, f"%{specialty_keyword}%")

async def lookup_active_appointment(pool, phone):
    query = """
        SELECT a.id, p.name as patient_name, d.name as doctor_name, a.appointment_start
        FROM appointments a
        JOIN patients p ON a.patient_id = p.id
        JOIN doctors d ON a.doctor_id = d.id
        WHERE p.phone = $1 AND a.status IN ('pending', 'confirmed')
        ORDER BY a.appointment_start DESC LIMIT 1
    """
    return await pool.fetchrow(query, phone)

async def get_doctor_booked_slots(pool, doctor_id: str, date_obj):
    """Fetch booked start times using the correct 'appointment_start' column."""
    query = """
        SELECT TO_CHAR(appointment_start AT TIME ZONE 'Asia/Kolkata', 'HH12:MI AM') as time_str
        FROM appointments 
        WHERE doctor_id = $1::uuid 
          AND DATE(appointment_start AT TIME ZONE 'Asia/Kolkata') = $2
          AND (
              status = 'confirmed'
              OR (status = 'pending' AND created_at >= NOW() - INTERVAL '15 minutes')
          )
    """
    try:
        async with pool.acquire() as conn:
            records = await conn.fetch(query, doctor_id, date_obj)
            return [r['time_str'] for r in records]
    except Exception as e:
        print(f"Error fetching booked slots: {e}")
        return []

async def book_new_appointment(pool, clinic_id, doctor_id, patient_name, phone, start_time, end_time, force_book=False):
    """Handle 15-min cleanup, Patient creation/lookup, and Appointment booking with Safe UPSERT."""
    async with pool.acquire() as conn:
        # 1. Clean up old unpaid appointments
        cleanup_query = """
            UPDATE appointments 
            SET status = 'cancelled' 
            WHERE status = 'pending' AND created_at < NOW() - INTERVAL '15 minutes'
        """
        await conn.execute(cleanup_query)

        # 2. Get or Create the Patient
        patient_query = """
            INSERT INTO patients (clinic_id, name, phone)
            VALUES ($1, $2, $3)
            ON CONFLICT (clinic_id, phone) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
        """
        patient_id = await conn.fetchval(patient_query, clinic_id, patient_name, phone)

        # 3. OVERALL PATIENT APPOINTMENT CHECK (Blocks booking unless force_book is True)
        if not force_book:
            check_existing = """
                SELECT TO_CHAR(appointment_start AT TIME ZONE 'Asia/Kolkata', 'HH12:MI AM'), status 
                FROM appointments 
                WHERE patient_id = $1 
                  AND appointment_start >= NOW() 
                  AND status IN ('pending', 'confirmed')
                LIMIT 1
            """
            existing = await conn.fetchrow(check_existing, patient_id)
            if existing:
                # Returns a special string with the time and status
                return f"HAS_OTHER_APPOINTMENT|{existing[0]}|{existing[1]}"

        # 4. SAFEGUARD: Check if the specific slot is currently held by someone else
        check_query = """
            SELECT patient_id FROM appointments
            WHERE doctor_id = $1 AND appointment_start = $2
            AND (
                status = 'confirmed' 
                OR (status = 'pending' AND created_at >= NOW() - INTERVAL '15 minutes')
            )
        """
        existing_patient = await conn.fetchval(check_query, doctor_id, start_time)

        if existing_patient:
            if str(existing_patient) == str(patient_id):
                return "ALREADY_BOOKED_BY_USER"
            else:
                return "SLOT_TAKEN"

        # 5. Create the Appointment
        insert_query = """
            INSERT INTO appointments (clinic_id, patient_id, doctor_id, appointment_start, appointment_end, status)
            VALUES ($1, $2, $3, $4, $5, 'pending')
            ON CONFLICT (doctor_id, appointment_start) 
            DO UPDATE SET 
                patient_id = EXCLUDED.patient_id,
                appointment_end = EXCLUDED.appointment_end,
                status = 'pending',
                created_at = NOW()
            RETURNING id
        """
        appt_id = await conn.fetchval(insert_query, clinic_id, patient_id, doctor_id, start_time, end_time)
        
        return appt_id