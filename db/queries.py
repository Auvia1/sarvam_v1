from loguru import logger


async def get_or_create_patient(conn, clinic_id: str, patient_name: str, phone: str) -> str:
    """
    Finds an existing patient by phone and exact name.
    If the name is different (e.g., a family member), it safely creates a new profile.
    """
    from loguru import logger
    
    clean_name = patient_name.strip()
    clean_phone = phone.strip()
    
    # 1. Check if this exact person already exists
    find_query = """
        SELECT id FROM patients 
        WHERE clinic_id = $1 AND phone = $2 AND LOWER(name) = LOWER($3)
    """
    row = await conn.fetchrow(find_query, clinic_id, clean_phone, clean_name)
    
    if row:
        logger.info(f"👤 Found existing patient: {clean_name} ({clean_phone})")
        return str(row['id'])
        
    # 2. Not found! Create a new patient profile (even if the phone exists for someone else)
    insert_query = """
        INSERT INTO patients (clinic_id, name, phone) 
        VALUES ($1, $2, $3) 
        RETURNING id
    """
    new_id = await conn.fetchval(insert_query, clinic_id, clean_name, clean_phone)
    
    logger.info(f"🆕 Created NEW patient profile: {clean_name} ({clean_phone})")
    return str(new_id)

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
        records = await conn.fetch(query, clinic_id, f"%{specialty_keyword}%")
        
        # --- NEW LOGS ADDED HERE ---
        logger.info(f"🏥 DB FETCH: Searched for '{specialty_keyword}'. Found {len(records)} schedule rows.")
        for r in records:
            logger.info(f"   -> Doc: {r['name']} | DayOfWeek(0=Sun, 6=Sat): {r['day_of_week']} | Hours: {r['start_time']} - {r['end_time']}")
        # ---------------------------
        
        return records
async def lookup_active_appointment(pool, phone):
    """Fetches confirmed, or pending appointments strictly under 15 minutes old."""
    query = """
        SELECT a.id, p.name as patient_name, d.name as doctor_name, a.appointment_start
        FROM appointments a
        JOIN patients p ON a.patient_id = p.id
        JOIN doctors d ON a.doctor_id = d.id
        WHERE p.phone = $1 
          AND (
              a.status = 'confirmed' 
              OR (a.status = 'pending' AND a.created_at >= NOW() - INTERVAL '15 minutes')
          )
        ORDER BY a.created_at DESC LIMIT 1
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

async def book_new_appointment(pool, clinic_id, doctor_id, patient_name, phone, start_time, end_time, force_book=False, patient_id=None):
    """Handle 15-min cleanup, Patient creation/lookup, and Appointment booking with Safe UPSERT."""
    async with pool.acquire() as conn:
        # 1. Clean up old unpaid appointments by marking them as cancelled (Soft Delete)
        cleanup_query = """
            UPDATE appointments
            SET status = 'cancelled', updated_at = NOW()
            WHERE status = 'pending' AND created_at < NOW() - INTERVAL '15 minutes'
        """
        await conn.execute(cleanup_query)

        # 2. Get or Create the Patient (exact-name aware)
        resolved_patient_id = patient_id or await get_or_create_patient(conn, clinic_id, patient_name, phone)

        # 3. OVERALL PATIENT APPOINTMENT CHECK (Blocks booking unless force_book is True)
        if not force_book:
            check_existing = """
                SELECT 
                    a.id, 
                    TO_CHAR(a.appointment_start AT TIME ZONE 'Asia/Kolkata', 'HH12:MI AM') as time_str, 
                    a.status,
                    d.name as doctor_name,
                    d.speciality as doctor_speciality
                FROM appointments a
                JOIN doctors d ON a.doctor_id = d.id
                WHERE a.patient_id = $1 
                  AND a.appointment_start >= NOW() 
                  AND a.status IN ('pending', 'confirmed')
                ORDER BY a.created_at DESC
                LIMIT 1
            """
            existing = await conn.fetchrow(check_existing, resolved_patient_id)
            if existing:
                # Now returns the ID, Time, Status, Doc Name, and Doc Specialty!
                return f"HAS_OTHER_APPOINTMENT|{existing['id']}|{existing['time_str']}|{existing['status']}|{existing['doctor_name']}|{existing['doctor_speciality']}"

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
            if str(existing_patient) == str(resolved_patient_id):
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
        appt_id = await conn.fetchval(insert_query, clinic_id, resolved_patient_id, doctor_id, start_time, end_time)
        
        return appt_id