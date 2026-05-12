from __future__ import annotations

import logging

from aiogram import types
from aiogram.dispatcher.filters import ChatTypeFilter

from loader import dp, bot
from utils.db import (
    upsert_group, get_group, set_group_unit,
    get_drivers, add_driver, remove_driver, is_registered_driver,
)


def _setup_incomplete(group: dict | None) -> bool:
    return group is None or not group["setup_complete"]


async def _notify_incomplete_setup(chat_id: int):
    drivers = await get_drivers(chat_id)
    if not drivers:
        await bot.send_message(
            chat_id,
            "Setup incomplete. Have the driver send any message, then an admin replies to it:\n"
            "<code>/adddriver Driver Name</code>",
            parse_mode="HTML",
        )
    else:
        await bot.send_message(
            chat_id,
            "Setup incomplete. Please set the current unit number:\n"
            "<code>/setunit &lt;unit_number&gt;</code>",
            parse_mode="HTML",
        )


@dp.message_handler(content_types=types.ContentType.NEW_CHAT_MEMBERS)
async def on_bot_added(message: types.Message):
    bot_user = await bot.get_me()
    if not any(m.id == bot_user.id for m in message.new_chat_members):
        return

    await upsert_group(message.chat.id)
    await message.answer(
        "👋 Hello! To get started:\n\n"
        "1️⃣ Have the driver send any message in this group\n"
        "2️⃣ Admin replies to that message with:\n"
        "   <code>/adddriver Driver Name</code>\n\n"
        "Up to 2 drivers can be registered (for team drivers). "
        "After adding driver(s), set the current truck unit:\n\n"
        "<code>/setunit &lt;unit_number&gt;</code>",
        parse_mode="HTML",
    )


@dp.message_handler(commands=["adddriver"], chat_type=[types.ChatType.GROUP, types.ChatType.SUPERGROUP])
async def cmd_add_driver(message: types.Message):
    admin = await message.chat.get_member(message.from_user.id)
    if admin.status not in ("administrator", "creator"):
        await message.reply("Only group administrators can register drivers.")
        return

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
    # Drop a leading @username token if the admin included one — the user comes from the reply
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

    user = reply.from_user

    existing = await get_drivers(message.chat.id)
    if len(existing) >= 2:
        names = " & ".join(d["name"] for d in existing)
        await message.reply(
            f"This group already has 2 registered drivers: <b>{names}</b>.\n\n"
            "To replace one, reply to their message with:\n"
            "<code>/removedriver</code>",
            parse_mode="HTML",
        )
        return

    await upsert_group(message.chat.id)
    added = await add_driver(message.chat.id, user.id, driver_name)
    if not added:
        await message.reply(f"{driver_name} is already registered in this group.")
        return

    drivers = await get_drivers(message.chat.id)
    group = await get_group(message.chat.id)
    codriver_note = (
        "\n\nTo add a co-driver, have them send a message and reply with:\n"
        "<code>/adddriver Co-Driver Name</code>"
        if len(drivers) < 2 else ""
    )

    if not group or not group["unit_number"]:
        await message.reply(
            f"✅ {driver_name} registered.{codriver_note}\n\n"
            "Now set the current unit number:\n"
            "<code>/setunit &lt;unit_number&gt;</code>",
            parse_mode="HTML",
        )
    else:
        await message.reply(
            f"✅ {driver_name} registered.{codriver_note}",
            parse_mode="HTML",
        )


@dp.message_handler(commands=["setunit"], chat_type=[types.ChatType.GROUP, types.ChatType.SUPERGROUP])
async def cmd_set_unit(message: types.Message):
    admin = await message.chat.get_member(message.from_user.id)
    if admin.status not in ("administrator", "creator"):
        await message.reply("Only group administrators can set the unit number.")
        return

    unit = message.get_args().strip()
    if not unit:
        await message.reply(
            "Usage: <code>/setunit &lt;unit_number&gt;</code>",
            parse_mode="HTML",
        )
        return

    drivers = await get_drivers(message.chat.id)
    if not drivers:
        await message.reply(
            "No drivers registered yet. Have the driver send a message, then reply to it:\n"
            "<code>/adddriver Driver Name</code>",
            parse_mode="HTML",
        )
        return

    await upsert_group(message.chat.id)
    await set_group_unit(message.chat.id, unit)

    driver_names = " & ".join(d["name"] for d in drivers)
    await message.reply(f"✅ Setup complete. Unit <b>{unit}</b> assigned to {driver_names}.", parse_mode="HTML")


@dp.message_handler(commands=["removedriver"], chat_type=[types.ChatType.GROUP, types.ChatType.SUPERGROUP])
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

    await message.reply(f"✅ Driver removed.")
