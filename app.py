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
from handlers.admin.units import title_sweep_loop
from handlers.groups.proposals import schedule_pending_reminders, setup_nag_loop
from utils.gemini import get_active_model, set_active_model
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
    if stored_model and not set_active_model(stored_model):
        # A stored id that is no longer offered silently leaves the default in
        # place, which is how a fleet ends up on a model nobody chose.
        logging.warning("stored gemini_model %r is not selectable; staying on %s",
                        stored_model, get_active_model())
    # Log which model is actually live. The 2026-08-20 outage ran for days on the
    # dearest model on the menu with nothing anywhere saying so -- the failover
    # switches in memory, and the panel is the only other place it shows.
    logging.info("Gemini model in use: %s", get_active_model())
    await set_default_commands(dispatcher)
    await on_startup_notify(dispatcher)
    asyncio.create_task(compliance_loop())
    asyncio.create_task(setup_nag_loop())
    asyncio.create_task(title_sweep_loop())
    # utils/pti_retry.py is deliberately NOT started. Its re-runs were louder
    # than the failures they recovered: every attempt posts its own status
    # message, so one Gemini outage on 2026-08-31 left five "the analysis
    # service is overloaded" messages in a DM World group for a single
    # walkaround, hours apart, none of them an inspection result. A driver who
    # is told to send /check again does exactly that, and that path already
    # works. The module and its tests are kept for the 2026-08-20 case (every
    # key locked out of the model, nobody re-sending because nothing worked);
    # re-enable by restoring the create_task here and the enqueue in
    # handlers/groups/pti.py -- but not before a retry stops narrating itself
    # into the group.
    await schedule_pending_reminders()


if __name__ == '__main__':
    executor.start_polling(dp, on_startup=on_startup)
