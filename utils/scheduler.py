import asyncio
import logging

from utils.enforcement import run_compliance_check

INTERVAL_SECONDS = 3600  # hourly


async def compliance_loop():
    while True:
        await asyncio.sleep(INTERVAL_SECONDS)
        try:
            await run_compliance_check()
        except Exception:
            logging.exception("Compliance check failed")
