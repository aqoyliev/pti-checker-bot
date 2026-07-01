"""Admin driver-verification flow (button-driven).

A private-chat ``/verifydrivers`` walks every group in the ``driver_verify``
queue (seeded from a userbot member snapshot) one at a time:

    1. show the group title → "Is this a gurman group?"  [✅ Yes] [❌ No]
       • No  → mark skipped, go to next group.
    2. Yes → show the group's members as 3-column buttons → "Choose driver".
    3. tap a member → "Send the driver's full name" (text).
    4. → show members again → "Choose co-driver"  [⏭ Skip / none].
    5. tap a member → "Send the co-driver's full name" (text); Skip → finalize.
    6. commit driver (+ co-driver) to group_drivers, mark done, go to next.

Only bots can send inline keyboards, so this lives on the PTI bot. Member data
was captured out-of-band by a userbot (the bot API can't list members) and
seeded into ``driver_verify``. Callbacks are namespaced ``vf:`` and re-checked
against the admin allow-list. The current group's member list is held in FSM
state, keyed by index in the callback data (member ids exceed the 64-byte
callback limit when combined).
"""
from __future__ import annotations

import logging
from html import escape

from aiogram import types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from loader import dp
from utils.db import (
    get_next_verify,
    get_verify_progress,
    replace_drivers,
    set_verify_status,
)

# Reuse the admin gate from the panel so both share one allow-list.
from handlers.admin.panel import _is_admin

PRIVATE = types.ChatType.PRIVATE
MEMBER_COLS = 3


class VerifySG(StatesGroup):
    driver_name = State()      # waiting for the driver's full name (text)
    codriver_name = State()    # waiting for the co-driver's full name (text)


# ---------- keyboards ----------

def _member_kb(members: list[dict], action: str, skip: bool) -> InlineKeyboardMarkup:
    """3-column grid of members; callback ``vf:<action>:<idx>``. Optional Skip row."""
    kb = InlineKeyboardMarkup(row_width=MEMBER_COLS)
    buttons = []
    for idx, m in enumerate(members):
        label = m.get("name") or (f"@{m['username']}" if m.get("username") else str(m["user_id"]))
        buttons.append(InlineKeyboardButton(label[:32], callback_data=f"vf:{action}:{idx}"))
    kb.add(*buttons)
    if skip:
        kb.add(InlineKeyboardButton("⏭ No co-driver / skip", callback_data="vf:cod:skip"))
    kb.add(InlineKeyboardButton("✖️ Stop verifying", callback_data="vf:stop"))
    return kb


def _yesno_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Yes", callback_data="vf:grp:yes"),
        InlineKeyboardButton("❌ No", callback_data="vf:grp:no"),
    )
    kb.add(InlineKeyboardButton("✖️ Stop verifying", callback_data="vf:stop"))
    return kb


# ---------- flow steps ----------

async def _present_next(target: types.Message, state: FSMContext) -> None:
    """Load the next pending group and ask the yes/no question. `target` is a
    Message we can answer on (either the command message or a callback message)."""
    await state.finish()
    row = await get_next_verify()
    handled, total = await get_verify_progress()
    if not row:
        await target.answer(
            f"✅ Verification complete — nothing pending.\n{handled}/{total} groups handled."
        )
        return

    members = row["members"] or []
    await state.update_data(gid=row["group_id"], members=members)
    title = escape(row["title"] or "(no title)")
    unit = escape(str(row["unit"] or "—"))
    await target.answer(
        f"<b>[{handled + 1}/{total}] Unit {unit}</b>\n"
        f"<code>{title}</code>\n\n"
        f"Is this a gurman group?",
        reply_markup=_yesno_kb(),
    )


async def _ask_driver(query: types.CallbackQuery, state: FSMContext, *, role: str) -> None:
    data = await state.get_data()
    members = data.get("members") or []
    action = "drv" if role == "driver" else "cod"
    prompt = "Choose the <b>driver</b> account:" if role == "driver" else "Choose the <b>co-driver</b> account:"
    await query.message.edit_text(prompt, reply_markup=_member_kb(members, action, skip=(role == "codriver")))
    await query.answer()


# ---------- entrypoints ----------

@dp.message_handler(commands=["verifydrivers"], chat_type=PRIVATE, state="*")
async def cmd_verify(message: types.Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        return  # silent for non-admins
    await _present_next(message, state)


# ---------- callback router ----------

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("vf:"), state="*")
async def verify_cb(query: types.CallbackQuery, state: FSMContext):
    if not await _is_admin(query.from_user.id):
        await query.answer("Not authorized.", show_alert=True)
        return

    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""

    if action == "stop":
        await state.finish()
        await query.message.edit_text("Stopped. Send /verifydrivers to resume.")
        await query.answer()
        return

    if action == "grp":
        data = await state.get_data()
        gid = data.get("gid")
        if gid is None:
            await query.answer("Session expired — /verifydrivers to restart.", show_alert=True)
            return
        if arg == "no":
            await set_verify_status(int(gid), "skipped")
            await query.answer("Skipped.")
            await _present_next(query.message, state)
            return
        # yes → pick the driver
        await _ask_driver(query, state, role="driver")
        return

    if action == "drv":
        data = await state.get_data()
        members = data.get("members") or []
        try:
            m = members[int(arg)]
        except (ValueError, IndexError):
            await query.answer("Stale selection.", show_alert=True)
            return
        await state.update_data(driver_uid=m["user_id"])
        await VerifySG.driver_name.set()
        # keep gid/members in state across the state switch
        await state.update_data(gid=data.get("gid"), members=members)
        tg_name = escape(m.get("name") or str(m["user_id"]))
        await query.message.edit_text(
            f"Driver: <b>{tg_name}</b>\n\nSend the driver's <b>full name</b> as a text message."
        )
        await query.answer()
        return

    if action == "cod":
        data = await state.get_data()
        if arg == "skip":
            await _finalize(query.message, state, codriver=None)
            await query.answer("No co-driver.")
            return
        members = data.get("members") or []
        try:
            m = members[int(arg)]
        except (ValueError, IndexError):
            await query.answer("Stale selection.", show_alert=True)
            return
        await state.update_data(codriver_uid=m["user_id"])
        await VerifySG.codriver_name.set()
        await state.update_data(**{k: data.get(k) for k in ("gid", "members", "driver_uid", "driver_name")})
        tg_name = escape(m.get("name") or str(m["user_id"]))
        await query.message.edit_text(
            f"Co-driver: <b>{tg_name}</b>\n\nSend the co-driver's <b>full name</b>, or /skipname for none."
        )
        await query.answer()
        return

    await query.answer()


# ---------- text-input steps ----------

@dp.message_handler(state=VerifySG.driver_name, chat_type=PRIVATE)
async def verify_driver_name(message: types.Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("Empty — send the driver's full name.")
        return
    data = await state.get_data()
    await state.update_data(driver_name=name)
    members = data.get("members") or []
    # move to co-driver selection
    await state.update_data(**{k: data.get(k) for k in ("gid", "driver_uid")}, members=members, driver_name=name)
    await message.answer(
        f"Driver name saved: <b>{escape(name)}</b>.\n\nNow choose the <b>co-driver</b> (or skip):",
        reply_markup=_member_kb(members, "cod", skip=True),
    )


@dp.message_handler(state=VerifySG.codriver_name, chat_type=PRIVATE)
async def verify_codriver_name(message: types.Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    codriver_name = None if text == "/skipname" else text
    if codriver_name == "":
        await message.answer("Empty — send the co-driver's full name or /skipname.")
        return
    await _finalize(message, state, codriver=codriver_name)


async def _finalize(target: types.Message, state: FSMContext, *, codriver: str | None) -> None:
    """Commit the driver (+ co-driver) and advance to the next group."""
    data = await state.get_data()
    gid = data.get("gid")
    driver_uid = data.get("driver_uid")
    driver_name = data.get("driver_name")
    if gid is None or driver_uid is None or not driver_name:
        await target.answer("Session expired — send /verifydrivers to restart.")
        await state.finish()
        return

    drivers = [{"user_id": driver_uid, "name": driver_name}]
    if codriver is not None:
        codriver_uid = data.get("codriver_uid")
        if codriver_uid and codriver_uid != driver_uid:
            drivers.append({"user_id": codriver_uid, "name": codriver})

    await replace_drivers(int(gid), drivers)
    await set_verify_status(int(gid), "done")
    names = " & ".join(escape(d["name"]) for d in drivers)
    await target.answer(f"✅ Saved: {names}")
    logging.info("verify: group %s drivers set to %s", gid, [(d["user_id"], d["name"]) for d in drivers])
    await _present_next(target, state)
