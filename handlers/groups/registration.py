from __future__ import annotations

import logging
from html import escape

from aiogram import types

from loader import dp, bot
from handlers.admin.onboard import start_onboarding
from utils.db import (
    upsert_group, get_group, set_group_unit,
    get_drivers, add_driver, remove_driver,
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
    "2. Set the unit number: <code>/setunit &lt;unit_number&gt;</code>"
)


@dp.message_handler(content_types=types.ContentType.NEW_CHAT_MEMBERS)
async def on_bot_added(message: types.Message):
    bot_user = await bot.get_me()
    if not any(m.id == bot_user.id for m in message.new_chat_members):
        return

    await upsert_group(message.chat.id)
    await message.answer(INTRO_MESSAGE, parse_mode="HTML")

    # Drivers are no longer asked to configure anything. The bot reads the unit
    # off the title and hands the admins a member picker in DM instead. This
    # reverses the earlier #1/#2 rule ("never read the group's name") — the
    # title is now used, but only as a *suggestion* an admin confirms, because
    # it is 79.5% accurate and sometimes names a different valid unit.
    try:
        await start_onboarding(message.chat.id, message.chat.title or "")
        return
    except Exception:
        logging.exception("onboarding prompt failed for %s; falling back to manual",
                          message.chat.id)

    # Fallback only: admins unreachable or the userbot is unconfigured.
    await message.answer(MANUAL_SETUP_MESSAGE, parse_mode="HTML")


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

    # Replying to one of the bot's own setup messages would otherwise register
    # the bot as a driver of its own group -- it then counts toward the quota
    # and gets tagged in reminders.
    if reply.from_user.is_bot:
        await message.reply(
            "That's a bot, not a driver. Reply to a message the "
            "<b>driver</b> sent.",
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
        await message.reply("That user is already registered as a driver.")
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

    await upsert_group(message.chat.id)
    added = await add_driver(message.chat.id, reply.from_user.id, driver_name)
    if not added:
        await message.reply(f"{escape(driver_name)} was already registered; no change.", parse_mode="HTML")
        return

    group = await get_group(message.chat.id)
    if group and group.get("unit_number"):
        await set_group_unit(message.chat.id, group["unit_number"])  # flips setup_complete = TRUE
        drivers = await get_drivers(message.chat.id)
        names = " & ".join(d["name"] for d in drivers)
        await message.reply(
            f"✅ {escape(driver_name)} registered.\n"
            f"Setup complete: unit <b>{escape(group['unit_number'])}</b> assigned to {escape(names)}.",
            parse_mode="HTML",
        )
    else:
        await message.reply(
            f"✅ {escape(driver_name)} registered. Now set the unit number:\n"
            f"<code>/setunit &lt;unit_number&gt;</code>",
            parse_mode="HTML",
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

    await upsert_group(message.chat.id)
    await set_group_unit(message.chat.id, unit)  # flips setup_complete = TRUE
    drivers = await get_drivers(message.chat.id)
    if drivers:
        names = " & ".join(d["name"] for d in drivers)
        await message.reply(
            f"✅ Setup complete. Unit <b>{escape(unit)}</b> assigned to {escape(names)}.",
            parse_mode="HTML",
        )
    else:
        await message.reply(
            f"✅ Unit <b>{escape(unit)}</b> saved. Now register the driver(s) — have them send a message, "
            f"then anyone reply with <code>/adddriver Driver Name</code>.",
            parse_mode="HTML",
        )


@dp.message_handler(commands=["removedriver"], chat_type=GROUP_TYPES)
async def cmd_remove_driver(message: types.Message):
    # #3/#6: anyone can add or remove drivers — no admin check, no confirmation.
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
