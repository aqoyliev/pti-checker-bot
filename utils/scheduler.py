import asyncio
import logging

from utils.enforcement import run_compliance_check
from utils.reminders import run_reminder_pass

INTERVAL_SECONDS = 3600  # hourly


async def compliance_loop():
    while True:
        await asyncio.sleep(INTERVAL_SECONDS)
        # Reminders (#8/#9) and the enforcement sweep (a no-op while
        # ENFORCEMENT_ENABLED is off) are independent — a failure in one must
        # not skip the other.
        try:
            await run_reminder_pass()
        except Exception:
            logging.exception("Reminder pass failed")
        try:
            await run_compliance_check()
        except Exception:
            logging.exception("Compliance check failed")
