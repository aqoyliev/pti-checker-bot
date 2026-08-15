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
from utils import phone_lookup, userbot
from utils.auto_onboard import plan_auto_config
from utils.driver_names import parse_driver_names
from utils.db import (
    clear_non_drivers,
    get_active_units,
    get_drivers,
    get_group,
    get_non_driver_ids,
    mark_non_drivers,
    replace_drivers,
    set_group_unit,
    unmark_non_drivers,
    upsert_group,
)
from utils.unit_parse import guess_unit, looks_retired

# group_id -> pending onboarding state, keyed per admin so two admins editing
# the same group don't clobber each other. In memory on purpose: this is a
# short interactive exchange, and a restart just means re-running /onboard.
_pending: dict[tuple[int, int], dict] = {}

MAX_DRIVERS = 2
# Member buttons per row, and how much of a name fits in one that narrow.
MEMBER_COLUMNS = 2
LABEL_CHARS = 24
# How many members fit in one prompt: at MEMBER_COLUMNS per row that is 20 rows
# of buttons plus the control rows, which is about all a Telegram message holds.
# This bounds the *keyboard* only. It must never bound the roster used to decide
# whether a resolved account is in the group -- these groups run to 60 members,
# so a driver sitting at position 41 would read as "not in this chat" and an
# otherwise-clean automatic setup would decline. Membership questions get the
# full roster; only the buttons get sliced.
#
# The slice is therefore applied *last*, to the already-filtered list (_shown),
# never to the roster on the way into the state. Slicing first produced a prompt
# with no buttons at all whenever the first 40 members were all known
# non-drivers: the drivers sat at position 41+ and were dropped before the
# non-driver filter ever ran, so the admin was told "40 non-drivers hidden" and
# given nobody to tap.
MEMBER_BUTTONS = 40
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


def _shown(st: dict) -> list:
    """The members actually on screen: the visible ones, capped to the keyboard.

    Everything user-facing derives from this rather than from `_visible`, so a
    roster longer than the keyboard can never be judged as though the admin had
    seen all of it.
    """
    return _visible(st)[:MEMBER_BUTTONS]


def _hidden_count(st: dict) -> int:
    return len(st["members"]) - len(_visible(st))


def _overflow_count(st: dict) -> int:
    """Visible members that didn't fit on the keyboard."""
    return len(_visible(st)) - len(_shown(st))


def _passed_over(st: dict) -> list[tuple[int, str]]:
    """Members the admin looked at and did not pick — the non-driver candidates.

    Only what was *on screen* counts. Someone hidden was never judged here, and
    someone past the button cap was never displayed either, so neither may be
    swept into a fleet-wide non-driver decision.
    """
    return [(m.user_id, m.label) for m in _shown(st)
            if m.user_id not in st["selected"]]


def _keyboard(admin_id: int, group_id: int) -> InlineKeyboardMarkup:
    st = _pending[_key(admin_id, group_id)]
    kb = InlineKeyboardMarkup(row_width=MEMBER_COLUMNS)

    # Two per row: a staff-heavy group runs to dozens of names, and one button
    # per row pushes Save off the bottom of the screen. Labels are cut shorter
    # to match the narrower button.
    people = [
        InlineKeyboardButton(
            f"{_marker(st['selected'], m.user_id)} {m.label}"[:LABEL_CHARS],
            callback_data=f"ob:t:{group_id}:{m.user_id}",
        )
        for m in _shown(st)
    ]
    for i in range(0, len(people), MEMBER_COLUMNS):
        kb.row(*people[i:i + MEMBER_COLUMNS])

    # Escape hatch: someone marked "not a driver" in another group may well be
    # the driver here, and without this they would be unreachable.
    hidden = _hidden_count(st)
    if hidden:
        kb.row(InlineKeyboardButton(f"👥 Show {hidden} hidden",
                                    callback_data=f"ob:a:{group_id}:0"))
    elif st.get("show_all") and st["members"]:
        kb.row(InlineKeyboardButton("🙈 Hide known non-drivers",
                                    callback_data=f"ob:a:{group_id}:0"))
    # Refresh is the fix for the commonest failure: the userbot account was not
    # in the group when the prompt was built. Add it, tap this, get the roster.
    kb.row(InlineKeyboardButton("🔄 Refresh members",
                                callback_data=f"ob:r:{group_id}:0"))
    kb.row(
        InlineKeyboardButton("✏️ Change unit", callback_data=f"ob:u:{group_id}:0"),
        InlineKeyboardButton("💾 Save", callback_data=f"ob:s:{group_id}:0"),
    )
    kb.row(InlineKeyboardButton("✖️ Skip this group", callback_data=f"ob:x:{group_id}:0"))
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
    # Why this group is being asked about rather than configured from the
    # About text's phone numbers. Without it, "why am I still tapping names?"
    # has no answer short of reading the logs.
    if st.get("auto_note"):
        lines.append(f"ℹ️ Couldn't do this automatically: {escape(st['auto_note'])}.")
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
        if not _shown(st):
            # Everyone in the roster is on the fleet-wide non-driver list, so
            # the prompt has nothing to tap. Say so plainly — the alternative is
            # a list of controls with no names above it and no explanation.
            lines.append("⚠️ Every member is on the non-driver list, so there is "
                         "nobody to pick. Tap <b>Show hidden</b> to see them "
                         "anyway.")
        overflow = _overflow_count(st)
        if overflow:
            lines.append(f"<i>Showing the first {len(_shown(st))} — {overflow} "
                         f"more member(s) didn't fit. Use /adddriver in the "
                         f"group for anyone missing.</i>")
        for uid in st["selected"]:
            member = next((x for x in st["members"] if x.user_id == uid), None)
            name = member.label if member else str(uid)
            lines.append(f"{_marker(st['selected'], uid)} {escape(name)}")
    return "\n".join(lines)


async def _try_auto_config(group_id: int, unit: str | None, description: str,
                           members: list) -> tuple[object | None, str]:
    """Resolve the About text's phone numbers into drivers. (plan, reason).

    Every failure is an ordinary "ask the admin instead", including the lookup
    being unavailable -- a rate-limited account must never be the reason a
    group gets configured wrongly, only the reason it gets configured by hand.
    """
    if not phone_lookup.is_configured():
        return None, ""
    phones = phone_lookup.extract_phones(description)
    if not phones:
        return None, "no phone numbers in the About text"
    try:
        resolved = await phone_lookup.lookup(phones)
    except phone_lookup.LookupUnavailable as e:
        logging.warning("auto-config for %s fell back to the picker: %s", group_id, e)
        return None, f"phone lookup unavailable ({e})"
    return plan_auto_config(unit, phones, resolved, members,
                            parse_driver_names(description))


def _auto_keyboard(group_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("✏️ Edit / undo",
                                callback_data=f"ob:e:{group_id}:0"))
    return kb


async def _apply_auto_config(group_id: int, plan) -> str:
    """Write the unit and drivers, and return the notice for the admins."""
    await set_group_unit(group_id, plan.unit)
    await replace_drivers(group_id, [{"user_id": uid, "name": label}
                                     for uid, label in plan.drivers])
    # Being chosen as a driver outranks a stale "not a driver" row, exactly as
    # it does on the Save path. Nobody was shown a picker here, so nobody is
    # marked as a non-driver: only people actually passed over count.
    await unmark_non_drivers([uid for uid, _ in plan.drivers])

    lines = [f"🤖 <b>Unit {escape(plan.unit)} configured automatically.</b>"]
    for user_id, name in plan.drivers:
        # The stored name is the fleet's, so the Telegram one is shown beside it
        # when they differ: names and numbers are paired by position, and this
        # is the line on which a swapped pair becomes visible.
        tg = plan.tg_labels.get(user_id)
        also = f", Telegram: {escape(tg)}" if tg and tg != name else ""
        lines.append(f"• {escape(name)} — <code>{user_id}</code> "
                     f"(from {escape(plan.sources[user_id])}{also})")
    lines.append("\n<i>Drivers were read from the phone numbers in the group's "
                 "About text. Tap Edit if any of this is wrong.</i>")
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
    roster = [m for m in members if not m.is_bot]
    unit, unit_source = await _checked_guess(title, description)
    hidden = await get_non_driver_ids()

    # The About text usually names both drivers by phone number. When all of it
    # checks out the group configures itself and the admins are told; anything
    # unclear falls through to the prompt below, with the reason attached.
    plan, auto_note = await _try_auto_config(group_id, unit, description, roster)
    if plan is not None:
        notice = await _apply_auto_config(group_id, plan)
        delivered = False
        for admin_id in _ADMIN_IDS:
            try:
                await bot.send_message(admin_id, notice, parse_mode="HTML",
                                       reply_markup=_auto_keyboard(group_id))
                delivered = True
            except Exception:
                logging.exception("could not tell admin %s about the auto-config "
                                  "of %s", admin_id, group_id)
        # The group is configured either way; an unreachable admin is not a
        # reason to leave it unconfigured, and there is nothing to nag about.
        return True

    delivered = False
    for admin_id in _ADMIN_IDS:
        key = _key(admin_id, group_id)
        _pending[key] = {
            "title": title,
            "description": description,
            "unit": unit,
            "unit_source": unit_source,
            "retired_marker": looks_retired(title),
            "members": roster,
            "hidden": hidden,
            "show_all": False,
            "selected": [],
            "auto_note": auto_note,
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


async def _rebuild_pending(admin_id: int, group_id: int) -> dict | None:
    """Recreate the state for a prompt whose process is gone. None if we can't.

    Selections are not recoverable — nothing was written — so this returns a
    fresh prompt rather than pretending otherwise.
    """
    try:
        chat = await bot.get_chat(group_id)
    except Exception as e:
        logging.warning("cannot rebuild onboarding state for %s: %s",
                        group_id, type(e).__name__)
        return None

    title = chat.title or str(group_id)
    members = await userbot.list_members(group_id)
    description = await userbot.get_description(group_id)
    unit, unit_source = await _checked_guess(title, description)

    st = {
        "title": title,
        "description": description,
        "unit": unit,
        "unit_source": unit_source,
        "retired_marker": looks_retired(title),
        "members": [m for m in members if not m.is_bot],
        "hidden": await get_non_driver_ids(),
        "show_all": False,
        "selected": [],
        "auto_note": "",
    }
    _pending[_key(admin_id, group_id)] = st
    return st


class OnboardSG(StatesGroup):
    unit = State()


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("ob:"))
async def on_onboard_click(call: types.CallbackQuery, state: FSMContext):
    _, action, gid_s, uid_s = call.data.split(":", 3)
    group_id, user_id = int(gid_s), int(uid_s)
    key = _key(call.from_user.id, group_id)

    st = _pending.get(key)
    if st is None:
        # The pending state lives in memory, so every deploy strands whatever
        # prompts are open. Rebuilding it costs one roster read and leaves the
        # admin where they were, which beats telling them to start over — all
        # the more so now each group is prompted only once.
        st = await _rebuild_pending(call.from_user.id, group_id)
        if st is None:
            await call.answer("I can't reach that group any more — run /onboard "
                              "again.", show_alert=True)
            return
        await call.answer("Prompt restored.")

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
        roster = [m for m in members if not m.is_bot]
        st["members"] = roster
        st["description"] = await userbot.get_description(group_id)
        st["hidden"] = await get_non_driver_ids()
        # Membership is judged against the whole roster, not the buttons: a pick
        # that scrolled past the slice is still a member, and dropping it here
        # would silently undo the admin's choice.
        present = {m.user_id for m in roster}
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

    if action == "e":
        # Edit/undo on an auto-configured group. The stored drivers are
        # pre-selected, so the admin corrects a pick rather than redoing the
        # group from scratch; nothing is unwritten until they Save.
        stored = await get_drivers(group_id)
        present = {m.user_id for m in st["members"]}
        st["selected"] = [d["user_id"] for d in stored
                          if d["user_id"] in present][:MAX_DRIVERS]
        st["auto_note"] = ""
        await call.message.edit_text(
            _text(call.from_user.id, group_id), parse_mode="HTML",
            reply_markup=_keyboard(call.from_user.id, group_id))
        await call.answer("Change the picks, then Save.")
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
        # A driver already registered here keeps the name they were stored with.
        # This prompt is also the Edit path for a group configured from its
        # About text, where the stored name is the fleet's -- re-saving to fix
        # the *other* pick must not quietly swap it back to a Telegram handle.
        kept = {d["user_id"]: d["name"] for d in await get_drivers(group_id)
                if d.get("name")}
        names = []
        picked = []
        for uid in st["selected"]:
            m = next((x for x in st["members"] if x.user_id == uid), None)
            name = kept.get(uid) or (m.label if m else str(uid))
            picked.append({"user_id": uid, "name": name})
            names.append(name)
        # Replace rather than add: this prompt is also the Edit path for a group
        # that configured itself from the About text, and a correction there has
        # to remove the driver it is correcting, not sit alongside them.
        await replace_drivers(group_id, picked)

        # Everyone the admin looked at and passed over is a non-driver —
        # dispatch, safety, the owner — and they recur across groups, so
        # remember them and stop offering them. Only people who were actually
        # on screen count: someone hidden behind "Show hidden" was never judged
        # here, and the selected drivers are cleared from the list outright.
        newly_hidden = await mark_non_drivers(_passed_over(st))
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
    if st is None and group_id is not None:
        st = await _rebuild_pending(message.from_user.id, group_id)
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
    """Open the prompt for a group — including one the bot never registered.

    The bot only creates a row when it is *added* to a group, so a group it was
    already sitting in when this feature shipped has no row at all. Refusing
    those with "Unknown group" would lock out exactly the groups that most need
    onboarding, so membership is checked against Telegram rather than the DB.
    """
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

    # getChat is the real test of "can I work with this chat", and it fails for
    # a group the bot is not in — which is the only case worth refusing.
    try:
        chat = await bot.get_chat(group_id)
    except Exception as e:
        await message.answer(
            f"I can't reach that chat ({type(e).__name__}). Am I still in it?")
        return

    if not await get_group(group_id):
        await upsert_group(group_id)
        await message.answer(f"Registering <code>{group_id}</code> — it had no "
                             f"record yet.", parse_mode="HTML")

    if not await start_onboarding(group_id, chat.title or str(group_id)):
        await message.answer("Couldn't send you the prompt — check the logs.")
