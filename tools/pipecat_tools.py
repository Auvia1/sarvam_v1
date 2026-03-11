from pipecat.adapters.schemas.tools_schema import ToolsSchema

# Global DB Pool for tools
_pool = None

def init_tool_db(pool):
    global _pool
    _pool = pool

def get_pool():
    return _pool

# Import tools after definition to avoid circular imports
from tools.availability import check_availability
from tools.booking import book_appointment
from tools.reschedule import lookup_appointment, reschedule_appointment
from tools.cancel import cancel_appointment
from tools.notify import send_payment_link
from tools.language import switch_language

def register_all_tools(llm):
    llm.register_direct_function(check_availability, cancel_on_interruption=False)
    llm.register_direct_function(book_appointment, cancel_on_interruption=False)
    llm.register_direct_function(lookup_appointment, cancel_on_interruption=False)
    llm.register_direct_function(reschedule_appointment, cancel_on_interruption=False)
    llm.register_direct_function(cancel_appointment, cancel_on_interruption=False)
    llm.register_direct_function(send_payment_link, cancel_on_interruption=False)
    llm.register_direct_function(switch_language, cancel_on_interruption=False)

def get_tools_schema():
    return ToolsSchema(standard_tools=[
        check_availability,
        book_appointment,
        lookup_appointment,
        reschedule_appointment,
        cancel_appointment,
        send_payment_link,
        switch_language
    ])