import os
import requests
from loguru import logger


def _format_whatsapp_number(phone_number: str) -> str:
    digits_only = "".join(filter(str.isdigit, str(phone_number)))
    if len(digits_only) == 10:
        return f"91{digits_only}"
    if digits_only.startswith("91") and len(digits_only) == 12:
        return digits_only
    return digits_only


async def send_confirmation(phone_number: str, message: str):
    """Sends a WhatsApp message using Meta's Official Cloud API."""
    meta_access_token = os.getenv("META_ACCESS_TOKEN") or os.getenv("WHATSAPP_ACCESS_TOKEN")
    meta_phone_number_id = os.getenv("META_PHONE_NUMBER_ID") or os.getenv("WHATSAPP_PHONE_ID")

    if not meta_access_token or not meta_phone_number_id:
        logger.error("⚠️ Meta WhatsApp credentials missing in .env")
        return False

    formatted_number = _format_whatsapp_number(phone_number)

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
        import httpx
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=payload)

            if response.status_code in [200, 201]:
                logger.info(f"✅ Meta WhatsApp message sent to {formatted_number}!")
                return True

            logger.error(f"❌ Meta WhatsApp Error {response.status_code}: {response.text}")
            return False

    except Exception as e:
        logger.error(f"❌ Meta WhatsApp request failed: {e}")
        return False


async def send_interactive_slots(phone_number: str, doc_name: str, date_str: str, slots: list):
    """Sends a WhatsApp Interactive List message with available time slots."""
    meta_access_token = os.getenv("WHATSAPP_ACCESS_TOKEN") or os.getenv("META_ACCESS_TOKEN")
    meta_phone_number_id = os.getenv("WHATSAPP_PHONE_ID") or os.getenv("META_PHONE_NUMBER_ID")

    if not meta_access_token or not meta_phone_number_id:
        logger.error("⚠️ Meta WhatsApp credentials missing in .env")
        return False

    formatted_number = _format_whatsapp_number(phone_number)
    url = f"https://graph.facebook.com/v22.0/{meta_phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {meta_access_token}",
        "Content-Type": "application/json",
    }

    display_slots = slots[:10]
    rows = []
    for slot in display_slots:
        rows.append({
            "id": f"SLOT_{slot}",
            "title": slot,
        })

    payload = {
        "messaging_product": "whatsapp",
        "to": formatted_number,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {
                "text": f"👨‍⚕️ *{doc_name}* is available on {date_str}.\n\nPlease select a time slot below:",
            },
            "action": {
                "button": "View Available Slots",
                "sections": [
                    {
                        "title": "Available Times",
                        "rows": rows,
                    }
                ],
            },
        },
    }

    try:
        import httpx

        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers)
            if response.status_code in [200, 201]:
                logger.info(f"✅ Interactive slot list sent to {formatted_number}!")
                return True

            logger.error(f"❌ Failed to send interactive slots: {response.text}")
            return False
    except Exception as e:
        logger.error(f"❌ Interactive slot request failed: {e}")
        return False