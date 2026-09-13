"""Re-send the onboarding prompt for groups that are still unconfigured.

A group the bot joined but nobody set up shows nothing in the chat -- drivers
are never asked to configure anything -- so the only thing that can move it
along is an admin. This loop finds such groups once a minute and hands each
one to ``start_onboarding``, which DMs the admins the member picker (or
configures the group outright from its About text). It never posts into the
group itself.

Each group is prompted **once**: the ceiling lives in the query
(``get_groups_needing_setup_nag`` returns groups with ``setup_nag_count < 1``),
because the prompt is a DM with a picker in it, and re-sending turns an admin's
chat into a stack of identical prompts, all but the newest already dead. A
group that never got one is reachable with ``/onboard <group_id>``.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram.utils.exceptions import ChatNotFound, MethodIsNotAvailable, Unauthorized

from handlers.admin.onboard import start_onboarding
from loader import bot
from utils.db import (
    UNREACHABLE_LIMIT,
    bump_setup_nag,
    clear_unreachable,
    get_groups_needing_setup_nag,
    mark_group_inactive,
    mark_unreachable,
)

# How long after the bot joins (or after its last prompt) before this loop
# considers the group again. The join handler already sends the first prompt
# itself, so this only reaches groups whose join-time prompt reached nobody.
SETUP_NAG_INTERVAL = timedelta(minutes=10)

# Errors that mean the group is gone for good (kicked/blocked/deleted/not found).
# Unauthorized is the parent of BotKicked/BotBlocked and also covers the bare
# "Forbidden: the group chat was deleted" case.
_UNREACHABLE = (Unauthorized, ChatNotFound, MethodIsNotAvailable)


async def setup_nag_loop():
    while True:
        try:
            await _run_setup_nag_pass()
        except Exception:
            logging.exception("setup nag pass failed")
        await asyncio.sleep(60)


async def _run_setup_nag_pass():
    now = datetime.utcnow()
    for g in await get_groups_needing_setup_nag():
        last = g.get("last_setup_nag_at")
        if last is not None and (now - last) < SETUP_NAG_INTERVAL:
            continue
        try:
            # If no admin is reachable the group gets nothing -- silence is the
            # intended behaviour, not a fallback to nagging drivers.
            chat = await bot.get_chat(g["group_id"])
            if not await start_onboarding(g["group_id"], chat.title or str(g["group_id"])):
                logging.warning("setup nag for %s reached no admin; group left untouched",
                                g["group_id"])
            await bump_setup_nag(g["group_id"])
            await clear_unreachable(g["group_id"])
        except _UNREACHABLE as e:
            # Same rule as the reminder path: one failure is not proof the group
            # is gone (the local Bot API server forgets chats on restart), so
            # deactivate only after a sustained streak.
            strikes = await mark_unreachable(g["group_id"])
            if strikes >= UNREACHABLE_LIMIT:
                logging.warning(
                    "Group %s unreachable %s times in a row during setup nag (%s)"
                    " — deactivating", g["group_id"], strikes, type(e).__name__,
                )
                await mark_group_inactive(g["group_id"])
            else:
                logging.warning(
                    "Group %s unreachable during setup nag (%s), strike %s/%s",
                    g["group_id"], type(e).__name__, strikes, UNREACHABLE_LIMIT,
                )
        except Exception:
            logging.exception("failed to send setup nag to %s", g["group_id"])
