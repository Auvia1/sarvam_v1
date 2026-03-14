import os
import requests
from loguru import logger


async def send_confirmation(phone_number: str, message: str):
    """Sends a WhatsApp message using Meta's Official Cloud API."""
    meta_access_token = os.getenv("META_ACCESS_TOKEN")
    meta_phone_number_id = os.getenv("META_PHONE_NUMBER_ID")

    if not meta_access_token or not meta_phone_number_id:
        logger.error("⚠️ Meta WhatsApp credentials missing in .env")
        return False

    digits_only = "".join(filter(str.isdigit, str(phone_number)))
    if len(digits_only) == 10:
        formatted_number = f"91{digits_only}"
    elif digits_only.startswith("91") and len(digits_only) == 12:
        formatted_number = digits_only
    else:
        formatted_number = digits_only

    url = f"https://graph.facebook.com/v22.0/{meta_phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {meta_access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": formatted_number,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": message,
        },
    }

    try:
        response = requests.post(url, headers=headers, json=payload)

        if response.status_code == 200:
            logger.info(f"✅ Meta WhatsApp message sent to {formatted_number}!")
            return True

        logger.error(f"❌ Meta WhatsApp Error {response.status_code}: {response.json()}")
        return False

    except Exception as e:
        logger.error(f"❌ Meta WhatsApp request failed: {e}")
        return False