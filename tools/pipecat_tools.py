from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from tools.pool import init_tool_db, get_pool  # re-export so agent.py import path stays the same

from tools.availability import check_availability
from tools.booking import book_appointment, resend_payment_link
from tools.reschedule import lookup_appointment, reschedule_appointment
from tools.cancel import cancel_appointment
from tools.language import switch_language


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
        "force_book": {
            "type": "boolean",
            "description": "Set to true ONLY if the user explicitly confirmed they want to overwrite/add to their existing appointment.",
        },
    },
    required=["doctor_id", "patient_name", "start_time_iso", "phone"],
)

def register_all_tools(llm):
    llm.register_direct_function(check_availability, cancel_on_interruption=False)
    llm.register_direct_function(book_appointment, cancel_on_interruption=False)
    llm.register_direct_function(resend_payment_link, cancel_on_interruption=False)
    llm.register_direct_function(lookup_appointment, cancel_on_interruption=False)
    llm.register_direct_function(reschedule_appointment, cancel_on_interruption=False)
    llm.register_direct_function(cancel_appointment, cancel_on_interruption=False)
    llm.register_direct_function(switch_language, cancel_on_interruption=False)

def get_tools_schema():
    return ToolsSchema(standard_tools=[
        check_availability,
        book_appointment_schema,
        resend_payment_link,
        lookup_appointment,
        reschedule_appointment,
        cancel_appointment,
        switch_language
    ])