from __future__ import annotations

import logging
from html import escape

from aiogram import types

from loader import bot_id, dp
from handlers.admin.onboard import start_onboarding
from utils.admins import is_admin
from utils.driver_names import tidy_name
from utils.db import (
    upsert_group, get_group, set_group_unit,
    get_drivers, add_driver, remove_driver,
    bump_setup_nag, migrate_group_id,
)

GROUP_TYPES = [types.ChatType.GROUP, types.ChatType.SUPERGROUP]

INTRO_MESSAGE = (
    "👋 Hi! I'm the <b>PTI Checker Bot</b>.\n\n"
    "I review pre-trip inspection photos and videos and decide PASS / FAIL "
    "using a DOT-trained model. Reply to a driver's PTI media with <code>/check</code> "
    "and I'll do the rest. Type <code>/help</code> any time for the full guide.\n\n"
    # The one place this rule is visible rather than asserted: the message is
    # in the chat, above the videos it is talking about. Drivers otherwise
    # reply /check to the PTI filmed before anyone added the bot, and the
    # refusal they get is the first thing that tells them.
    "⚠️ I can only read what is posted <b>from here on</b> — anything above "
    "this message is out of my reach, so reply <code>/check</code> to a video "
    "sent after it."
)

# NOTE: /setunit and /adddriver are open to EVERY member of the group as of
# 2026-10-10, at the fleet's instruction, and /check's own refusals now name
# them. Both people a setup needs are already in that chat -- the driver, to be
# replied to, and whoever read the refusal -- and routing it through an admin
# who is not there is what left groups unconfigured for weeks while their
# drivers watched a bot that had stopped answering. The cost is known and
# accepted: a member can re-file the truck with one /setunit, and the daily
# title sweep is what corrects that.
#
# /removedriver stays admin-only, and off the group command menu. It is the one
# that takes a driver *out* of compliance, nothing about setting a group up
# needs it, and /adddriver's own reply names it only for the group that already
# has two drivers.

_NOT_ADMIN = "Only the fleet's admins can change a group's setup."


async def _admin_only(message: types.Message) -> bool:
    if message.from_user and await is_admin(message.from_user.id):
        return True
    await message.reply(_NOT_ADMIN)
    return False


@dp.message_handler(content_types=[types.ContentType.MIGRATE_TO_CHAT_ID,
                                   types.ContentType.MIGRATE_FROM_CHAT_ID])
async def on_chat_migrated(message: types.Message):
    """Follow a group that Telegram upgraded to a supergroup.

    The upgrade replaces the chat id outright, and the bot is not re-added, so
    nothing else in the flow ever learns the new one: the old id keeps taking
    reminders that bounce, and a PTI sent in the new chat is refused by
    ``_group_ready`` because no ``groups`` row matches it -- silently, which is
    what makes this worth catching at the service message rather than waiting
    for something to fail.

    Telegram announces the upgrade twice, once on each side, and both are
    handled: the pair is the same either way round, and ``migrate_group_id`` is
    a no-op the second time.
    """
    if message.migrate_to_chat_id:
        old_id, new_id = message.chat.id, message.migrate_to_chat_id
    else:
        old_id, new_id = message.migrate_from_chat_id, message.chat.id
    if await migrate_group_id(old_id, new_id):
        logging.info("group %s was upgraded to a supergroup; moved it to %s",
                     old_id, new_id)


@dp.message_handler(content_types=types.ContentType.NEW_CHAT_MEMBERS)
async def on_bot_added(message: types.Message):
    me = await bot_id()
    if not any(m.id == me for m in message.new_chat_members):
        return

    await upsert_group(message.chat.id)
    await message.answer(INTRO_MESSAGE, parse_mode="HTML")

    # Drivers are no longer asked to configure anything. The bot reads the unit
    # off the title/description and hands the admins a member picker in DM
    # instead. This reverses the earlier #1/#2 rule ("never read the group's
    # name") — the title is now used, but only as a *suggestion* an admin
    # confirms, because it is 79.5% accurate and sometimes names a different
    # valid unit.
    #
    # If no admin can be reached the group is left alone on purpose: nothing is
    # posted asking anyone to register. The group stays unconfigured and shows
    # up in the setup nag / `/onboard <group_id>` instead.
    try:
        if await start_onboarding(message.chat.id, message.chat.title or ""):
            # This *is* the group's one prompt, so record it against the nag
            # budget — otherwise the loop sends an identical second one ten
            # minutes later. A prompt that reached nobody is deliberately not
            # counted, leaving the loop one retry.
            await bump_setup_nag(message.chat.id)
        else:
            logging.warning(
                "group %s (%r) joined but no admin could be prompted — it stays "
                "unconfigured until an admin runs /onboard",
                message.chat.id, message.chat.title,
            )
    except Exception:
        logging.exception("onboarding prompt failed for %s", message.chat.id)


@dp.message_handler(commands=["adddriver"], chat_type=GROUP_TYPES)
async def cmd_add_driver(message: types.Message):
    # Open to every member -- see the NOTE above.
    args = message.get_args().strip()
    reply = message.reply_to_message

    if not reply or not reply.from_user:
        await message.reply(
            "Reply to the driver's own message with <code>/adddriver</code> — "
            "that reply is how I learn which account is theirs.",
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
    # The shape it will be stored in, so the confirmation below names the
    # driver the way every report will.
    driver_name = tidy_name(driver_name)

    if not driver_name:
        # Telegram's own name rather than a refusal (2026-10-10). A bare
        # /adddriver replying to the driver has already said who is meant, and
        # the name is only a label -- the fleet's own one is better, which is
        # what reading the About text is for, and the panel's rename,
        # /fixnames and a per-group /onboard all exist to replace this one
        # later. Refusing instead left the group with no driver at all, which
        # is the state that stops /check.
        driver_name = tidy_name(reply.from_user.full_name or "")

    if not driver_name:
        await message.reply(
            "Please include the driver's name: "
            "<code>/adddriver Driver Name</code>",
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
            f"This group already has 2 registered drivers: "
            f"<b>{escape(names)}</b>.\n\n"
            "To replace one, a fleet admin can reply "
            "<code>/removedriver</code> to their message.",
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
            f"✅ {escape(driver_name)} registered. Now name the truck:\n"
            f"<code>/setunit 1234</code>",
            parse_mode="HTML",
        )


@dp.message_handler(commands=["setunit"], chat_type=GROUP_TYPES)
async def cmd_set_unit(message: types.Message):
    # Open to every member -- see the NOTE above.
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
            f"✅ Unit <b>{escape(unit)}</b> saved. Now register the "
            f"driver(s): reply <code>/adddriver</code> to a message each of "
            f"them sent.",
            parse_mode="HTML",
        )


@dp.message_handler(commands=["removedriver"], chat_type=GROUP_TYPES)
async def cmd_remove_driver(message: types.Message):
    if not await _admin_only(message):
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
