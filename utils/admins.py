"""Who counts as a bot admin.

Two tiers, resolved the same way everywhere (/admin, the web panel, the group
setup commands):

* the ids in the ``ADMINS`` env var are **super-admins** -- always, without a
  database row, so a fresh deployment has someone who can open the panel;
* everyone in the ``admins`` table is an admin; the row says whether they are
  super. Regular admins are added from the panel at runtime.

Super-admins manage other admins and are the only ones who see the AI model
at all (2026-10-14: a regular admin is not shown which model runs, let alone
offered a switch); regular admins can do everything else the panel offers.
"""
from __future__ import annotations

import logging

from data.config import ADMINS
from utils.db import get_admin

SUPER_ADMIN_IDS = frozenset(
    int(a) for a in ADMINS if str(a).strip().lstrip("-").isdigit())


async def resolve_admin(user_id: int) -> dict | None:
    """``{'user_id', 'is_super_admin'}`` for admins, None for everyone else."""
    if user_id in SUPER_ADMIN_IDS:
        return {"user_id": user_id, "is_super_admin": True}
    return await get_admin(user_id)


async def is_admin(user_id: int) -> bool:
    return await resolve_admin(user_id) is not None


async def is_super_admin(user_id: int) -> bool:
    row = await resolve_admin(user_id)
    return bool(row and row.get("is_super_admin"))


async def notify_super_admins(text: str, **kwargs) -> int:
    """DM every env-configured super-admin. Returns how many were reached.

    The one way the bot tells its operator something -- the compliance summary,
    the daily title sweep, a group it can no longer post in. It goes to the
    ``ADMINS`` ids rather than the whole ``admins`` table on purpose: those are
    the people who stood the deployment up, and a fleet manager added from the
    panel to fix driver rows has no use for "the bot lost posting rights in
    unit 2570".

    Never raises: an admin who has not started a DM with the bot cannot be
    reached, and that must not sink the caller's pass.
    """
    from loader import bot  # local: keeps this module importable without aiogram

    kwargs.setdefault("parse_mode", "HTML")
    sent = 0
    for admin_id in SUPER_ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, **kwargs)
            sent += 1
        except Exception:
            logging.warning("could not reach admin %s", admin_id, exc_info=True)
    return sent


def _same_name(a: str | None, b: str | None) -> bool:
    return " ".join((a or "").split()).casefold() == " ".join((b or "").split()).casefold()


async def notify_admins_named(name: str, text: str, **kwargs) -> int:
    """DM the admins whose Telegram name is `name`. Returns how many were reached.

    For the reports a fleet's safety desk acts on and its owner does not want
    -- the morning PTI call's (2026-10-14: "only the admin called Safety
    Monitoring, not me"). Super-admins are left out on purpose, whatever they
    are called. Matched by the name on the account, since that is how the fleet
    named the recipient; an account the bot cannot read (it never started a DM
    with this bot) cannot be messaged either, and is only logged.

    Never raises, same as `notify_super_admins`.
    """
    from loader import bot
    from utils.db import get_admins

    kwargs.setdefault("parse_mode", "HTML")
    sent = 0
    for row in await get_admins():
        user_id = row["user_id"]
        if user_id in SUPER_ADMIN_IDS or row.get("is_super_admin"):
            continue
        try:
            chat = await bot.get_chat(user_id)
        except Exception as e:
            logging.info("admin %s can't be read (%s) -- not a %r candidate",
                         user_id, type(e).__name__, name)
            continue
        if not _same_name(chat.full_name, name):
            continue
        try:
            await bot.send_message(user_id, text, **kwargs)
            sent += 1
        except Exception:
            logging.warning("could not reach admin %s", user_id, exc_info=True)
    if not sent:
        logging.warning("no admin named %r could be reached; the report was "
                        "not delivered", name)
    return sent
