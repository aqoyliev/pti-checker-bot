import asyncio
import logging

from aiogram import executor

from loader import dp
import middlewares, filters, handlers
from utils.notify_admins import on_startup_notify
from utils.set_bot_commands import set_default_commands
from data.config import ADMINS
from utils.db import init_db, get_setting, seed_super_admins
from utils.scheduler import compliance_loop
from utils.pti_retry import pti_retry_loop
from handlers.admin.units import units_refresh_loop
from handlers.groups.proposals import schedule_pending_reminders, setup_nag_loop
from utils.gemini import set_active_model
from webapp.server import start_webapp


async def on_startup(dispatcher):
    await init_db()
    # Web admin panel (Telegram Mini App). A failure to bind the port must not
    # take the bot down — the inline /admin panel still works without it.
    try:
        await start_webapp()
    except Exception:
        logging.exception("web admin panel failed to start")
    # Seed the env ADMINS as super-admins in the DB-backed admins table; regular
    # admins are added from the panel at runtime.
    try:
        await seed_super_admins([int(a) for a in ADMINS if str(a).strip()])
    except Exception:
        pass
    stored_model = await get_setting("gemini_model")
    if stored_model:
        set_active_model(stored_model)  # ignored if it's not a known model id
    await set_default_commands(dispatcher)
    await on_startup_notify(dispatcher)
    asyncio.create_task(compliance_loop())
    asyncio.create_task(setup_nag_loop())
    asyncio.create_task(units_refresh_loop())
    # Inspections that failed while Gemini was down get re-run from here once it
    # is answering again -- see utils/pti_retry.py.
    asyncio.create_task(pti_retry_loop())
    await schedule_pending_reminders()


if __name__ == '__main__':
    executor.start_polling(dp, on_startup=on_startup)
