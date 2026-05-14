from __future__ import annotations

import logging
from html import escape

from aiogram import types

from loader import dp, bot
from utils.db import (
    upsert_group, get_group, set_group_unit,
    get_drivers, remove_driver,
    find_open_add_driver_proposal,
)
from utils.group_info_parser import extract_unit_and_names
from handlers.groups.proposals import (
    propose_set_unit,
    propose_add_driver,
)

GROUP_TYPES = [types.ChatType.GROUP, types.ChatType.SUPERGROUP]

INTRO_MESSAGE = (
    "👋 Hi! I'm the <b>PTI Checker Bot</b>.\n\n"
    "I review pre-trip inspection photos and videos and decide PASS / FAIL "
    "using a DOT-trained model. Reply to a driver's PTI media with <code>/check</code> "
    "and I'll do the rest. Type <code>/help</code> any time for the full guide."
)

MANUAL_SETUP_MESSAGE = (
    "This group is not configured yet. Anyone in the group can run:\n"
    "1. Have the driver send a message, then reply with: <code>/adddriver Driver Name</code>\n"
    "2. Set the unit number: <code>/setunit &lt;unit_number&gt;</code>\n\n"
    "Each command needs 3 confirmations from members before it takes effect."
)


@dp.message_handler(content_types=types.ContentType.NEW_CHAT_MEMBERS)
async def on_bot_added(message: types.Message):
    bot_user = await bot.get_me()
    if not any(m.id == bot_user.id for m in message.new_chat_members):
        return

    await upsert_group(message.chat.id)

    await message.answer(INTRO_MESSAGE, parse_mode="HTML")

    try:
        chat = await bot.get_chat(message.chat.id)
        title = chat.title or message.chat.title or ""
        description = chat.description or ""
    except Exception:
        logging.exception("Failed to fetch chat for auto-extract")
        title = message.chat.title or ""
        description = ""

    unit, drivers = extract_unit_and_names(title, description)

    if not unit and not drivers:
        await message.answer(MANUAL_SETUP_MESSAGE, parse_mode="HTML")
        return

    lines = ["I read this group's name and bio:"]
    if unit:
        await set_group_unit(message.chat.id, unit)
        lines.append(f"🚛 Unit <b>{escape(unit)}</b> — saved.")
        lines.append("   <i>To change it, use <code>/setunit &lt;unit_number&gt;</code> (needs 3 confirmations).</i>")
    else:
        lines.append("🚛 Unit — not found. Set it with <code>/setunit &lt;unit_number&gt;</code>.")

    if drivers:
        names_html = " / ".join(f"<b>{escape(d)}</b>" for d in drivers)
        lines.append(f"👤 Detected driver(s): {names_html}")
        lines.append("")
        lines.append(
            "To register each driver, have them send any message in this group, then "
            "anyone reply with:\n<code>/adddriver Driver Name</code>\n"
            "Each <code>/adddriver</code> needs <b>3 confirmations</b> from members."
        )
    else:
        lines.append("👤 Drivers — not detected. Have each driver send a message, then reply with "
                     "<code>/adddriver Driver Name</code> (needs 3 confirmations).")

    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message_handler(commands=["adddriver"], chat_type=GROUP_TYPES)
async def cmd_add_driver(message: types.Message):
    args = message.get_args().strip()
    reply = message.reply_to_message

    if not reply or not reply.from_user:
        await message.reply(
            "Reply to the driver's message with:\n"
            "<code>/adddriver Driver Name</code>",
            parse_mode="HTML",
        )
        return

    driver_name = args.strip()
    parts = driver_name.split(None, 1)
    if parts and parts[0].startswith("@"):
        driver_name = parts[1].strip() if len(parts) > 1 else ""

    if not driver_name:
        await message.reply(
            "Please include the driver's name.\n"
            "Usage: <code>/adddriver Driver Name</code>",
            parse_mode="HTML",
        )
        return

    existing = await get_drivers(message.chat.id)
    if any(d["user_id"] == reply.from_user.id for d in existing):
        await message.reply(f"That user is already registered as a driver.")
        return
    if len(existing) >= 2:
        names = " & ".join(d["name"] for d in existing)
        await message.reply(
            f"This group already has 2 registered drivers: <b>{names}</b>.\n\n"
            "To replace one, reply to their message with:\n"
            "<code>/removedriver</code>",
            parse_mode="HTML",
        )
        return

    pending = await find_open_add_driver_proposal(message.chat.id, reply.from_user.id)
    if pending:
        await message.reply(
            "There is already an open proposal to register this user. Please vote on it first."
        )
        return

    display = reply.from_user.full_name or (f"@{reply.from_user.username}" if reply.from_user.username else "this user")
    await propose_add_driver(
        chat_id=message.chat.id,
        driver_user_id=reply.from_user.id,
        driver_name=driver_name,
        display_name=display,
        proposer_id=message.from_user.id,
    )


@dp.message_handler(commands=["setunit"], chat_type=GROUP_TYPES)
async def cmd_set_unit(message: types.Message):
    unit = message.get_args().strip()
    if not unit:
        await message.reply(
            "Usage: <code>/setunit &lt;unit_number&gt;</code>",
            parse_mode="HTML",
        )
        return

    await propose_set_unit(
        chat_id=message.chat.id,
        unit=unit,
        proposer_id=message.from_user.id,
    )


@dp.message_handler(commands=["removedriver"], chat_type=GROUP_TYPES)
async def cmd_remove_driver(message: types.Message):
    admin = await message.chat.get_member(message.from_user.id)
    if admin.status not in ("administrator", "creator"):
        await message.reply("Only group administrators can remove drivers.")
        return

    reply = message.reply_to_message
    if not reply or not reply.from_user:
        drivers = await get_drivers(message.chat.id)
        if drivers:
            names = "\n".join(f"• {d['name']}" for d in drivers)
            await message.reply(
                f"Reply to the driver's message with <code>/removedriver</code> to remove them.\n\n"
                f"Current drivers:\n{names}",
                parse_mode="HTML",
            )
        else:
            await message.reply("No drivers are registered in this group.")
        return

    removed = await remove_driver(message.chat.id, reply.from_user.id)
    if not removed:
        await message.reply("That user is not a registered driver in this group.")
        return

    await message.reply("✅ Driver removed.")
