from pipecat.services.llm_service import FunctionCallParams
from loguru import logger

async def send_payment_link(params: FunctionCallParams, phone: str):
    """Send a WhatsApp payment link to the user to confirm their booking.
    Args:
        phone: 10-digit phone number.
    """
    logger.info(f"📲 Mock WhatsApp Payment link sent to {phone}")
    await params.result_callback({"status": "sent"})