import logging

from aiogram import types
from aiogram.dispatcher.filters import ChatTypeFilter

from loader import dp, bot
from utils.db import (
    upsert_group, get_group, set_group_unit,
    get_drivers, add_driver, is_registered_driver,
)


def _setup_incomplete(group: dict | None) -> bool:
    return group is None or not group["setup_complete"]


async def _notify_incomplete_setup(chat_id: int):
    drivers = await get_drivers(chat_id)
    if not drivers:
        await bot.send_message(
            chat_id,
            "Setup incomplete. Please register at least one driver:\n"
            "<code>/adddriver @username Driver Name</code>",
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
        "👋 Hello! To get started, register the driver(s) for this group:\n\n"
        "<code>/adddriver @username Driver Name</code>\n\n"
        "Up to 2 drivers can be registered (for team drivers). "
        "After adding driver(s), set the current truck unit:\n\n"
        "<code>/setunit &lt;unit_number&gt;</code>",
        parse_mode="HTML",
    )


@dp.message_handler(commands=["adddriver"], chat_type=[types.ChatType.GROUP, types.ChatType.SUPERGROUP])
async def cmd_add_driver(message: types.Message):
    admin = await message.chat.get_member(message.from_user.id)
    if admin.status not in ("administrator", "creator"):
        return

    args = message.get_args().strip()
    if not args:
        await message.reply(
            "Usage: <code>/adddriver @username Driver Name</code>",
            parse_mode="HTML",
        )
        return

    parts = args.split(None, 1)
    if len(parts) < 2:
        await message.reply(
            "Please provide both username and name.\n"
            "Usage: <code>/adddriver @username Driver Name</code>",
            parse_mode="HTML",
        )
        return

    username_arg, driver_name = parts[0].lstrip("@"), parts[1].strip()

    try:
        chat_member = await bot.get_chat_member(message.chat.id, f"@{username_arg}")
        user = chat_member.user
    except Exception:
        await message.reply(f"Could not find user @{username_arg} in this group.")
        return

    existing = await get_drivers(message.chat.id)
    if len(existing) >= 2:
        await message.reply("This group already has 2 registered drivers (maximum for team drivers).")
        return

    await upsert_group(message.chat.id)
    added = await add_driver(message.chat.id, user.id, driver_name)
    if not added:
        await message.reply(f"{driver_name} is already registered in this group.")
        return

    drivers = await get_drivers(message.chat.id)
    group = await get_group(message.chat.id)

    if not group or not group["unit_number"]:
        await message.reply(
            f"✅ {driver_name} registered.\n\n"
            "Now set the current unit number:\n"
            "<code>/setunit &lt;unit_number&gt;</code>",
            parse_mode="HTML",
        )
    else:
        await message.reply(f"✅ {driver_name} registered.")


@dp.message_handler(commands=["setunit"], chat_type=[types.ChatType.GROUP, types.ChatType.SUPERGROUP])
async def cmd_set_unit(message: types.Message):
    admin = await message.chat.get_member(message.from_user.id)
    if admin.status not in ("administrator", "creator"):
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
            "No drivers registered yet. Add a driver first:\n"
            "<code>/adddriver @username Driver Name</code>",
            parse_mode="HTML",
        )
        return

    await upsert_group(message.chat.id)
    await set_group_unit(message.chat.id, unit)

    driver_names = " & ".join(d["name"] for d in drivers)
    await message.reply(f"✅ Setup complete. Unit <b>{unit}</b> assigned to {driver_names}.", parse_mode="HTML")
