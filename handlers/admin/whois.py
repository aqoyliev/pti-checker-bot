"""/whois <phone…> — resolve phone numbers to Telegram accounts.

Admin-only, DM-only. The fleet's driver lists arrive as names and phone
numbers, but everything the bot does is keyed on user_id, so the gap between
"we know this driver's number" and "we can register them" is exactly this
lookup. See utils/phone_lookup.py for why it needs its own user session.

A number that resolves is also checked against group_drivers, since "already
registered on unit 1225" is usually the real question behind the ask.
"""
from __future__ import annotations

from html import escape

from aiogram import types

from data.config import ADMINS
from loader import dp
from utils import phone_lookup
from utils.db import get_driver_memberships

_ADMIN_IDS = [int(a) for a in ADMINS if str(a).strip().isdigit()]

MAX_NUMBERS = 10


def _format_match(raw: str, m: phone_lookup.Match, groups: list[dict]) -> str:
    uname = f" @{escape(m.username)}" if m.username else ""
    bot_tag = "  [bot]" if m.is_bot else ""
    lines = [f"<b>{escape(raw)}</b> → <code>{m.user_id}</code> "
             f"{escape(m.label)}{uname}{bot_tag}"]
    for g in groups:
        unit = g["unit_number"] or "no unit"
        state = "" if g["is_active"] else " (inactive)"
        lines.append(f"    driver on <b>{escape(str(unit))}</b>{state} "
                     f"— <code>{g['group_id']}</code>")
    if not groups:
        lines.append("    not registered as a driver anywhere")
    return "\n".join(lines)


@dp.message_handler(commands=["whois"], chat_type=types.ChatType.PRIVATE)
async def cmd_whois(message: types.Message):
    if message.from_user.id not in _ADMIN_IDS:
        return

    numbers = message.get_args().split()
    if not numbers:
        await message.answer(
            "Usage: <code>/whois 786-488-2619 [+15616747866 …]</code>\n"
            "Bare 10-digit numbers are treated as US.", parse_mode="HTML")
        return
    if len(numbers) > MAX_NUMBERS:
        await message.answer(f"Send at most {MAX_NUMBERS} numbers at a time — "
                             f"contact lookup is heavily rate-limited.")
        return

    unparseable = [n for n in numbers if not phone_lookup.normalize_phone(n)]
    if unparseable:
        await message.answer("Not phone numbers: " +
                             escape(", ".join(unparseable)))
        if len(unparseable) == len(numbers):
            return

    notice = await message.answer(f"Looking up {len(numbers)} number(s)…")
    try:
        results = await phone_lookup.lookup(numbers)
    except phone_lookup.LookupUnavailable as e:
        await notice.edit_text(f"Lookup unavailable — {escape(str(e))}",
                               parse_mode="HTML")
        return

    out = []
    for raw in numbers:
        m = results.get(raw)
        if m is None:
            out.append(f"<b>{escape(raw)}</b> → no account found "
                       f"(not on Telegram, or hidden by privacy settings)")
        else:
            out.append(_format_match(raw, m, await get_driver_memberships(m.user_id)))
    await notice.edit_text("\n".join(out), parse_mode="HTML")
