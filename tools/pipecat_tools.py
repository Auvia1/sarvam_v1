#tools/pipecat_tools.py
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from tools.pool import init_tool_db, get_pool  # re-export so agent.py import path stays the same

from tools.availability import check_availability
from tools.booking import book_appointment
from tools.reschedule import lookup_appointment
from tools.language import switch_language


check_availability_schema = FunctionSchema(
    name="check_availability",
    description="Checks doctor availability by specialty.",
    properties={
        "problem_or_speciality": {
            "type": "string",
            "description": "CRITICAL: You must map the user's symptoms to one of our exact official database titles: 'General Physician', 'Dermatologist', 'Cardiologist', 'Pediatrician', 'Orthopedic', or 'Urologist'. DO NOT use variations like 'Dermatology' or 'Pediatrics'."
        },
        "requested_date": {
            "type": "string",
            "description": "Optional. The specific date the user requested in YYYY-MM-DD format (e.g., '2026-03-20'). Only provide this if the user explicitly asks for a certain day or date."
        }
    },
    required=["problem_or_speciality"]
)


book_appointment_schema = FunctionSchema(
    name="book_appointment",
    description="Books the appointment. DO NOT call this until the user has explicitly spoken their name and phone number.",
    properties={
        "doctor_id": {
            "type": "string",
            "description": "The exact 36-character UUID of the doctor.",
        },
        "patient_name": {
            "type": "string",
            "description": "The name of the patient. STRICT RULE: You must collect this from the user. NEVER guess or use placeholders like 'John Doe'.",
        },
        "start_time_iso": {
            "type": "string",
            "description": "The start time in ISO 8601 format.",
        },
        "phone": {
            "type": "string",
            "description": "The 10-digit phone number. STRICT RULE: You must collect this from the user. NEVER guess or use fake numbers.",
        },
        "reason": {
            "type": "string",
            "description": "The medical problem or symptoms the user is experiencing (e.g., 'fever and cough'). Pull this from the earlier conversation.",
        },
        "force_book": {
            "type": "boolean",
            "description": "Set to true ONLY if the user explicitly confirmed they want to overwrite/add to their existing appointment.",
        },
    },
    required=["doctor_id", "patient_name", "start_time_iso", "phone", "reason"],
)

def register_all_tools(llm):
    llm.register_direct_function(check_availability, cancel_on_interruption=False)
    llm.register_direct_function(book_appointment, cancel_on_interruption=False)
    llm.register_direct_function(lookup_appointment, cancel_on_interruption=False)
    llm.register_direct_function(switch_language, cancel_on_interruption=False)

def get_tools_schema():
    return ToolsSchema(standard_tools=[
        check_availability_schema,
        book_appointment_schema,
        lookup_appointment,
        switch_language
    ])