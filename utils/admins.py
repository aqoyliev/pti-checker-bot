"""Who counts as a bot admin.

Two tiers, resolved the same way everywhere (/admin, the web panel, the group
setup commands):

* the ids in the ``ADMINS`` env var are **super-admins** -- always, without a
  database row, so a fresh deployment has someone who can open the panel;
* everyone in the ``admins`` table is an admin; the row says whether they are
  super. Regular admins are added from the panel at runtime.

Super-admins manage other admins and switch the AI model; regular admins can
do everything else the panel offers.
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
