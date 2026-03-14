"""
Quick test: sends a WhatsApp message via Meta Cloud API.
Run from project root:  python3 tests/test_whatsapp.py
"""
import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv(override=True)

from tools.notify import send_confirmation

TEST_PHONE = "8309833107"   # change if needed

async def main():
    print(f"Sending test WhatsApp to {TEST_PHONE} ...")
    result = await send_confirmation(
        TEST_PHONE,
        "✅ Test message from Mithra Hospitals. If you see this, Meta WhatsApp is working!"
    )
    print("Result:", "SUCCESS" if result else "FAILED")

asyncio.run(main())
