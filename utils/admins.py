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
