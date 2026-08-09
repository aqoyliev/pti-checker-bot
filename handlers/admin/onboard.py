"""Admin-driven group onboarding.

Replaces the old flow where the bot posted setup instructions into the driver's
group and nagged until someone ran /setunit and /adddriver. Drivers are never
asked to configure anything now. Instead, when the bot is added to a group it:

  1. guesses the unit from the chat title (a suggestion only -- see
     utils/unit_parse, title parsing is 79.5% accurate and sometimes names a
     *different* valid unit);
  2. reads the member roster through the userbot, since the Bot API cannot
     list members;
  3. DMs the admins the title, the description and one button per member.

The admin taps the drivers, confirms the unit, and the group is configured.
Nothing is written until they press Save, so a bad title guess can't reach the
database on its own.
"""
from __future__ import annotations

import logging
from html import escape

from aiogram import types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.exceptions import MessageNotModified

from data.config import ADMINS
from loader import bot, dp
from utils import userbot
from utils.db import (
    add_driver,
    clear_non_drivers,
    get_active_units,
    get_group,
    get_non_driver_ids,
    mark_non_drivers,
    set_group_unit,
    unmark_non_drivers,
)
from utils.unit_parse import guess_unit, looks_retired

# group_id -> pending onboarding state, keyed per admin so two admins editing
# the same group don't clobber each other. In memory on purpose: this is a
# short interactive exchange, and a restart just means re-running /onboard.
_pending: dict[tuple[int, int], dict] = {}

MAX_DRIVERS = 2
_ADMIN_IDS = [int(a) for a in ADMINS if str(a).strip().isdigit()]

# Picked drivers are numbered in the order they were tapped rather than all
# getting the same tick, so the admin can see at a glance that two *different*
# people were selected -- names in these groups are often near-identical.
_ORDINALS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣")
_UNPICKED = "▫️"


def _key(admin_id: int, group_id: int) -> tuple[int, int]:
    return (admin_id, group_id)


async def _checked_guess(title: str, description: str) -> tuple[str | None, str]:
    """Guess the unit, then keep it only if it is an active unit.

    A group title outlives the truck -- groups get renamed late, or never -- so
    a parsed number can name a unit that left the fleet months ago. Rather than
    offer it, fall back to "not found" and make the admin say what it is.
    An empty units list means none has been supplied yet, so the check is
    skipped rather than rejecting everything.
    """
    unit, source = guess_unit(title, description)
    if not unit:
        return None, ""
    active = await get_active_units()
    if active and unit not in active:
        logging.info("unit %s parsed from %s is not in the active list — ignoring",
                     unit, source)
        return None, ""
    return unit, source


def _marker(selected: list[int], user_id: int) -> str:
    """The button prefix for one member: 1️⃣/2️⃣ in pick order, else ▫️."""
    if user_id not in selected:
        return _UNPICKED
    position = selected.index(user_id)
    # Deselecting the first pick renumbers the rest, since `selected` is a list
    # and the marker is derived from it rather than stored on the member.
    return _ORDINALS[position] if position < len(_ORDINALS) else "✅"


def _visible(st: dict) -> list:
    """Members worth offering: everyone, minus known non-drivers.

    Someone already picked stays visible even if they are on the non-driver
    list, so a stale exclusion can never hide a pick that is currently made.
    """
    if st.get("show_all"):
        return st["members"]
    hidden = st.get("hidden", set())
    return [m for m in st["members"]
            if m.user_id not in hidden or m.user_id in st["selected"]]


def _hidden_count(st: dict) -> int:
    return len(st["members"]) - len(_visible(st))


def _keyboard(admin_id: int, group_id: int) -> InlineKeyboardMarkup:
    st = _pending[_key(admin_id, group_id)]
    kb = InlineKeyboardMarkup(row_width=1)
    for m in _visible(st):
        kb.add(InlineKeyboardButton(
            f"{_marker(st['selected'], m.user_id)} {m.label}"[:60],
            callback_data=f"ob:t:{group_id}:{m.user_id}",
        ))

    # Escape hatch: someone marked "not a driver" in another group may well be
    # the driver here, and without this they would be unreachable.
    hidden = _hidden_count(st)
    if hidden:
        kb.add(InlineKeyboardButton(f"👥 Show {hidden} hidden",
                                    callback_data=f"ob:a:{group_id}:0"))
    elif st.get("show_all") and st["members"]:
        kb.add(InlineKeyboardButton("🙈 Hide known non-drivers",
                                    callback_data=f"ob:a:{group_id}:0"))
    # Refresh is the fix for the commonest failure: the userbot account was not
    # in the group when the prompt was built. Add it, tap this, get the roster.
    kb.add(InlineKeyboardButton("🔄 Refresh members",
                                callback_data=f"ob:r:{group_id}:0"))
    kb.add(
        InlineKeyboardButton("✏️ Change unit", callback_data=f"ob:u:{group_id}:0"),
        InlineKeyboardButton("💾 Save", callback_data=f"ob:s:{group_id}:0"),
    )
    kb.add(InlineKeyboardButton("✖️ Skip this group", callback_data=f"ob:x:{group_id}:0"))
    return kb


def _text(admin_id: int, group_id: int) -> str:
    st = _pending[_key(admin_id, group_id)]
    unit = st["unit"] or "—"
    lines = [
        "🆕 <b>New group — who are the drivers?</b>",
        f"<b>Title:</b> {escape(st['title'])}",
    ]
    if st["description"]:
        lines.append(f"<b>About:</b> {escape(st['description'][:300])}")
    source = st.get("unit_source") or ""
    label = f"Unit (from {source})" if source else "Unit (not found)"
    lines.append(f"<b>{label}:</b> <code>{escape(str(unit))}</code>")
    if st.get("unit_inactive"):
        lines.append("⚠️ That unit is not in the active list — check it before "
                     "saving.")
    if st["retired_marker"]:
        lines.append("⚠️ The title says this group is inactive/moved — check "
                     "it is really the live chat before saving.")
    if not st["members"]:
        lines.append("\n⚠️ No member list available — the userbot account is "
                     "probably not in this group. Add it, then tap "
                     "<b>Refresh members</b>. (Or add drivers with /adddriver "
                     "in the group.)")
    else:
        lines.append(f"\nTap up to {MAX_DRIVERS} drivers, then Save. "
                     f"Selected: {len(st['selected'])}/{MAX_DRIVERS}")
        hidden = _hidden_count(st)
        if hidden:
            lines.append(f"<i>{hidden} known non-driver(s) hidden.</i>")
        for uid in st["selected"]:
            member = next((x for x in st["members"] if x.user_id == uid), None)
            name = member.label if member else str(uid)
            lines.append(f"{_marker(st['selected'], uid)} {escape(name)}")
    return "\n".join(lines)


async def start_onboarding(group_id: int, title: str) -> bool:
    """Called when the bot joins a group. Never messages the group itself.

    Returns True if at least one admin actually received the prompt. A bot
    cannot open a DM with someone who has never started it, so "no admin
    reachable" is an ordinary outcome, not an error -- and the caller has to
    know about it, or the group is left with no setup path at all.
    """
    members = await userbot.list_members(group_id)
    description = await userbot.get_description(group_id)
    drivers_pool = [m for m in members if not m.is_bot][:40]
    unit, unit_source = await _checked_guess(title, description)
    hidden = await get_non_driver_ids()

    delivered = False
    for admin_id in _ADMIN_IDS:
        key = _key(admin_id, group_id)
        _pending[key] = {
            "title": title,
            "description": description,
            "unit": unit,
            "unit_source": unit_source,
            "retired_marker": looks_retired(title),
            "members": drivers_pool,
            "hidden": hidden,
            "show_all": False,
            "selected": [],
        }
        try:
            await bot.send_message(
                admin_id, _text(admin_id, group_id),
                parse_mode="HTML", reply_markup=_keyboard(admin_id, group_id),
            )
            delivered = True
        except Exception:
            # Most often "bot can't initiate conversation with a user".
            logging.exception("could not send onboarding prompt to admin %s", admin_id)
            _pending.pop(key, None)

    if not delivered:
        logging.warning("no admin could be reached to onboard group %s", group_id)
    return delivered


class OnboardSG(StatesGroup):
    unit = State()


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("ob:"))
async def on_onboard_click(call: types.CallbackQuery, state: FSMContext):
    _, action, gid_s, uid_s = call.data.split(":", 3)
    group_id, user_id = int(gid_s), int(uid_s)
    key = _key(call.from_user.id, group_id)

    st = _pending.get(key)
    if st is None:
        await call.answer("This prompt expired — run /onboard again.", show_alert=True)
        return

    if action == "t":
        if user_id in st["selected"]:
            st["selected"].remove(user_id)
        elif len(st["selected"]) >= MAX_DRIVERS:
            await call.answer(f"Only {MAX_DRIVERS} drivers per group.", show_alert=True)
            return
        else:
            st["selected"].append(user_id)
        await call.message.edit_text(
            _text(call.from_user.id, group_id), parse_mode="HTML",
            reply_markup=_keyboard(call.from_user.id, group_id))
        await call.answer()
        return

    if action == "r":
        # The account was probably just added to the group. Re-read the roster
        # and the About text; keep whatever drivers were already picked, minus
        # anyone who is no longer a member.
        await call.answer("Checking…")
        members = await userbot.list_members(group_id)
        st["members"] = [m for m in members if not m.is_bot][:40]
        st["description"] = await userbot.get_description(group_id)
        st["hidden"] = await get_non_driver_ids()
        present = {m.user_id for m in st["members"]}
        st["selected"] = [uid for uid in st["selected"] if uid in present]

        # A description that was unreadable before may carry the unit now. Never
        # overwrite a unit the admin typed by hand.
        if st.get("unit_source") != "you":
            st["unit"], st["unit_source"] = await _checked_guess(
                st["title"], st["description"])

        try:
            await call.message.edit_text(
                _text(call.from_user.id, group_id), parse_mode="HTML",
                reply_markup=_keyboard(call.from_user.id, group_id))
        except MessageNotModified:
            # Refreshing an unchanged prompt is the normal "still nothing" case.
            pass
        if not st["members"]:
            await call.answer(
                "Still no member list. Add the userbot account to the group "
                "first, then tap Refresh again.", show_alert=True)
        return

    if action == "a":
        st["show_all"] = not st.get("show_all")
        await call.message.edit_text(
            _text(call.from_user.id, group_id), parse_mode="HTML",
            reply_markup=_keyboard(call.from_user.id, group_id))
        await call.answer()
        return

    if action == "u":
        await state.update_data(onboard_group=group_id)
        await OnboardSG.unit.set()
        await call.message.answer("Send the correct unit number for this group.")
        await call.answer()
        return

    if action == "x":
        _pending.pop(key, None)
        await call.message.edit_text("Skipped. Run /onboard to configure it later.")
        await call.answer()
        return

    if action == "s":
        if not st["unit"]:
            await call.answer("Set a unit first (Change unit).", show_alert=True)
            return
        if not st["selected"]:
            await call.answer("Select at least one driver.", show_alert=True)
            return

        await set_group_unit(group_id, st["unit"])
        names = []
        for uid in st["selected"]:
            m = next((x for x in st["members"] if x.user_id == uid), None)
            name = m.label if m else str(uid)
            await add_driver(group_id, uid, name)
            names.append(name)

        # Everyone the admin looked at and passed over is a non-driver —
        # dispatch, safety, the owner — and they recur across groups, so
        # remember them and stop offering them. Only people who were actually
        # on screen count: someone hidden behind "Show hidden" was never judged
        # here, and the selected drivers are cleared from the list outright.
        passed_over = [(m.user_id, m.label) for m in _visible(st)
                       if m.user_id not in st["selected"]]
        newly_hidden = await mark_non_drivers(passed_over)
        await unmark_non_drivers(st["selected"])
        _pending.pop(key, None)

        lines = [f"✅ Unit <code>{escape(st['unit'])}</code> configured.",
                 f"Drivers: {escape(', '.join(names))}"]
        if newly_hidden:
            lines.append(f"<i>{newly_hidden} other member(s) noted as non-drivers "
                         f"— they won't be offered again.</i>")
        await call.message.edit_text("\n".join(lines), parse_mode="HTML")
        await call.answer("Saved")


@dp.message_handler(state=OnboardSG.unit, chat_type=types.ChatType.PRIVATE)
async def on_unit_typed(message: types.Message, state: FSMContext):
    data = await state.get_data()
    group_id = data.get("onboard_group")
    key = _key(message.from_user.id, group_id)
    st = _pending.get(key)
    await state.finish()
    if st is None:
        await message.answer("That prompt expired — run /onboard again.")
        return
    st["unit"] = message.text.strip()
    st["unit_source"] = "you"
    # A hand-typed unit is never rejected — the admin may be configuring a truck
    # before it reaches the weekly list — but an off-list number is worth saying
    # out loud, since it is usually a typo.
    active = await get_active_units()
    st["unit_inactive"] = bool(active) and st["unit"] not in active
    await message.answer(_text(message.from_user.id, group_id), parse_mode="HTML",
                         reply_markup=_keyboard(message.from_user.id, group_id))


@dp.message_handler(commands=["nondrivers"], chat_type=types.ChatType.PRIVATE)
async def cmd_non_drivers(message: types.Message):
    """Inspect or empty the fleet-wide non-driver list.

    The list is what keeps dispatchers out of the picker, so it needs a way
    back: if a real driver ever gets marked by mistake they would otherwise be
    permanently unofferable in every group.
    """
    if message.from_user.id not in _ADMIN_IDS:
        return
    if message.get_args().strip().lower() == "clear":
        dropped = await clear_non_drivers()
        await message.answer(f"Cleared {dropped} non-driver(s). Everyone will be "
                             f"offered again.")
        return
    count = len(await get_non_driver_ids())
    await message.answer(
        f"{count} people are marked as non-drivers and hidden from the picker.\n"
        f"Send <code>/nondrivers clear</code> to forget them all.",
        parse_mode="HTML")


@dp.message_handler(commands=["onboard"], chat_type=types.ChatType.PRIVATE)
async def cmd_onboard(message: types.Message):
    """Re-open the prompt for any group that still has no unit or drivers."""
    if message.from_user.id not in _ADMIN_IDS:
        return
    args = message.get_args().strip()
    if not args:
        await message.answer("Usage: <code>/onboard &lt;group_id&gt;</code>",
                             parse_mode="HTML")
        return
    try:
        group_id = int(args)
    except ValueError:
        await message.answer("That doesn't look like a group id.")
        return

    group = await get_group(group_id)
    if not group:
        await message.answer("Unknown group.")
        return
    chat = await bot.get_chat(group_id)
    await start_onboarding(group_id, chat.title or str(group_id))
