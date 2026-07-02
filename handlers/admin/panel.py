"""Owner-only in-Telegram admin panel.

A private-chat ``/admin`` command opens an inline-keyboard panel restricted to
the user ids in ``ADMINS``. From it an admin can:

* browse every group the bot is in and inspect its config / recent PTIs;
* override the 3-vote group flow directly (set unit, remove driver,
  deactivate / reactivate a group);
* see a global compliance snapshot;
* broadcast a message to all active groups.

Everything reuses the existing aiogram + asyncpg stack — no new infra. All
callbacks are namespaced ``adm:`` and re-checked against ``ADMINS`` so a leaked
callback id can't be replayed by a non-admin.
"""
from __future__ import annotations

import logging
from html import escape

from aiogram import types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import ContentType, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.exceptions import MessageNotModified

from data.config import ADMINS, WEBAPP_URL
from loader import bot, dp
from utils.db import (
    add_admin,
    get_active_group_ids,
    get_admin,
    get_admins,
    get_all_groups,
    get_drivers,
    get_group,
    get_recent_ptis,
    remove_admin,
    remove_driver,
    set_group_active,
    set_group_notifications,
    set_group_unit,
    set_setting,
)
from utils.enforcement import REQUIRED_PER_WEEK, check_driver_compliance
from test_pti import AVAILABLE_GEMINI_MODELS, get_active_model, set_active_model

PAGE_SIZE = 8
MENU_TEXT = "<b>🛠 PTI Admin Panel</b>\n\nChoose a section:"

# ADMINS comes from env.list() as strings; normalise once for fast lookups.
_ADMIN_IDS = {str(a).strip() for a in ADMINS if str(a).strip()}

PRIVATE = types.ChatType.PRIVATE


class AdminSG(StatesGroup):
    set_unit = State()
    broadcast = State()
    add_admin_id = State()


async def _admin_row(user_id: int) -> dict | None:
    """Resolve an admin. env ADMINS are always treated as super-admins (and are
    also seeded into the admins table at startup); everyone else is looked up in
    the DB-backed admins table (#10)."""
    if str(user_id) in _ADMIN_IDS:
        return {"user_id": user_id, "is_super_admin": True}
    return await get_admin(user_id)


async def _is_admin(user_id: int) -> bool:
    return await _admin_row(user_id) is not None


async def _is_super(user_id: int) -> bool:
    row = await _admin_row(user_id)
    return bool(row and row.get("is_super_admin"))


# ---------- keyboards ----------

def _menu_kb(is_super: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    if WEBAPP_URL:
        # Telegram Mini App version of this panel (webapp/) — same admin set,
        # nicer UX. Requires WEBAPP_URL to be the public HTTPS URL of the bot's
        # web server; web_app buttons only work in private chats, which is where
        # /admin lives.
        kb.add(InlineKeyboardButton("🌐 Open Web Panel", web_app=WebAppInfo(url=WEBAPP_URL)))
    kb.add(
        InlineKeyboardButton("📋 Groups", callback_data="adm:groups:0"),
        InlineKeyboardButton("📊 Stats", callback_data="adm:stats"),
    )
    kb.add(InlineKeyboardButton("📣 Broadcast", callback_data="adm:bcast"))
    kb.add(InlineKeyboardButton("🤖 AI Model", callback_data="adm:model"))
    if is_super:
        kb.add(InlineKeyboardButton("🛡 Admins", callback_data="adm:admins"))
    kb.add(InlineKeyboardButton("✖️ Close", callback_data="adm:close"))
    return kb


def _back_kb(callback_data: str, label: str = "« Back") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup().add(InlineKeyboardButton(label, callback_data=callback_data))


def _group_label(g: dict) -> str:
    if not g.get("is_active", True):
        icon = "💤"
    elif g.get("setup_complete"):
        icon = "✅"
    else:
        icon = "⚙️"
    unit = g.get("unit_number") or "(no unit)"
    return f"{icon} {unit} · {g['group_id']}"


# ---------- helpers ----------

async def _edit(query: types.CallbackQuery, text: str, kb: InlineKeyboardMarkup | None):
    """Replace the panel message in place, falling back to a fresh message."""
    try:
        await query.message.edit_text(text, reply_markup=kb)
    except MessageNotModified:
        pass
    except Exception:
        try:
            await query.message.answer(text, reply_markup=kb)
        except Exception:
            logging.exception("admin panel: failed to render view")
    try:
        await query.answer()
    except Exception:
        pass


async def _chat_title(group_id: int) -> str:
    try:
        chat = await bot.get_chat(group_id)
        return chat.title or str(group_id)
    except Exception:
        return str(group_id)


# ---------- views (return text + keyboard) ----------

async def _render_groups(page: int) -> tuple[str, InlineKeyboardMarkup]:
    groups = await get_all_groups()
    total = len(groups)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chunk = groups[page * PAGE_SIZE : page * PAGE_SIZE + PAGE_SIZE]

    kb = InlineKeyboardMarkup(row_width=1)
    for g in chunk:
        kb.add(InlineKeyboardButton(_group_label(g), callback_data=f"adm:g:{g['group_id']}"))

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀", callback_data=f"adm:groups:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶", callback_data=f"adm:groups:{page + 1}"))
    if nav:
        kb.row(*nav)
    kb.add(InlineKeyboardButton("« Menu", callback_data="adm:menu"))

    text = f"<b>📋 Groups</b> ({total}) — page {page + 1}/{pages}"
    if not chunk:
        text += "\n\nNo groups yet."
    else:
        text += "\n\n✅ live · ⚙️ setup incomplete · 💤 inactive"
    return text, kb


async def _render_group(group_id: int) -> tuple[str, InlineKeyboardMarkup]:
    g = await get_group(group_id)
    if not g:
        return "Group not found.", _back_kb("adm:groups:0", "« Groups")

    title = escape(await _chat_title(group_id))
    drivers = await get_drivers(group_id)
    active = g.get("is_active", True)
    notif_off = g.get("notifications_disabled")

    unit = escape(g.get("unit_number") or "—")
    truck_plate = g.get("truck_plate")
    unit_line = f"{unit}" + (f" (plate {escape(truck_plate)})" if truck_plate else "")
    trailer_unit = g.get("trailer_unit")
    trailer_plate = g.get("trailer_plate")
    if trailer_unit or trailer_plate:
        trailer_line = escape(trailer_unit or "—") + (
            f" (plate {escape(trailer_plate)})" if trailer_plate else ""
        )
    else:
        trailer_line = "—"

    if drivers:
        driver_line = ", ".join(escape(d["name"]) for d in drivers)
    else:
        driver_line = "none registered"

    recent = await get_recent_ptis(group_id, 1)
    if recent:
        p = recent[0]
        when = p["submitted_at"].strftime("%Y-%m-%d %H:%M")
        last_line = f"{'✅ PASS' if p['passed'] else '❌ FAIL'} · {when}"
    else:
        last_line = "no PTIs yet"

    text = (
        f"🚛 <b>{title}</b>\n"
        f"ID: <code>{group_id}</code>\n"
        f"Status: {'🟢 active' if active else '💤 inactive'} · "
        f"{'setup complete' if g.get('setup_complete') else 'setup incomplete'}\n\n"
        f"Unit: <b>{unit_line}</b>\n"
        f"Trailer: {trailer_line}\n"
        f"Drivers: {driver_line}\n"
        f"Last PTI: {last_line}\n"
        f"Notifications: {'🔕 disabled' if notif_off else '🔔 on'}"
    )

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🔧 Set unit", callback_data=f"adm:su:{group_id}"),
        InlineKeyboardButton(f"👤 Drivers ({len(drivers)})", callback_data=f"adm:dr:{group_id}"),
    )
    kb.add(InlineKeyboardButton("📊 PTI log", callback_data=f"adm:plog:{group_id}"))
    kb.add(InlineKeyboardButton(
        "🔔 Enable notifications" if notif_off else "🔕 Disable notifications",
        callback_data=f"adm:notif:{group_id}",
    ))
    if active:
        kb.add(InlineKeyboardButton("🚫 Deactivate", callback_data=f"adm:tog:{group_id}"))
    else:
        kb.add(InlineKeyboardButton("✅ Reactivate", callback_data=f"adm:tog:{group_id}"))
    kb.add(InlineKeyboardButton("« Groups", callback_data="adm:groups:0"))
    return text, kb


async def _render_drivers(group_id: int) -> tuple[str, InlineKeyboardMarkup]:
    drivers = await get_drivers(group_id)
    kb = InlineKeyboardMarkup(row_width=1)
    for d in drivers:
        kb.add(
            InlineKeyboardButton(
                f"❌ Remove {d['name']}", callback_data=f"adm:drm:{group_id}:{d['user_id']}"
            )
        )
    kb.add(InlineKeyboardButton("« Group", callback_data=f"adm:g:{group_id}"))

    if drivers:
        body = "\n".join(f"• {escape(d['name'])} (<code>{d['user_id']}</code>)" for d in drivers)
    else:
        body = "No drivers registered."
    text = f"<b>👤 Drivers</b>\n\n{body}\n\nRemoving here skips the 3-vote process."
    return text, kb


async def _render_pti_log(group_id: int) -> tuple[str, InlineKeyboardMarkup]:
    ptis = await get_recent_ptis(group_id, 8)
    if ptis:
        lines = []
        for p in ptis:
            when = p["submitted_at"].strftime("%m-%d %H:%M")
            status = "✅" if p["passed"] else "❌"
            who = escape(str(p.get("driver_name") or p["user_id"]))
            unit = escape(str(p.get("unit_number") or "—"))
            lines.append(f"{status} {when} · {who} · {unit}")
        body = "\n".join(lines)
    else:
        body = "No PTIs recorded for this group."
    text = f"<b>📊 Recent PTIs</b>\n\n{body}"
    return text, _back_kb(f"adm:g:{group_id}", "« Group")


async def _render_stats() -> tuple[str, InlineKeyboardMarkup]:
    groups = await get_all_groups()
    active = [g for g in groups if g.get("is_active", True)]
    setup_done = [g for g in active if g.get("setup_complete")]
    inactive = len(groups) - len(active)

    total_drivers = 0
    compliant = 0
    non_compliant: list[str] = []
    for g in setup_done:
        gid = g["group_id"]
        unit = g.get("unit_number") or gid
        for d in await get_drivers(gid):
            total_drivers += 1
            ok, reason = await check_driver_compliance(gid, d["user_id"])
            if ok:
                compliant += 1
            else:
                non_compliant.append(f"• {escape(d['name'])} ({escape(str(unit))}) — {escape(reason)}")

    text = (
        f"<b>📊 Fleet stats</b>\n\n"
        f"Groups: {len(groups)} total · {len(active)} active · "
        f"{len(setup_done)} configured · {inactive} inactive\n"
        f"Drivers: {total_drivers} · compliant {compliant}/{total_drivers} "
        f"(quota {REQUIRED_PER_WEEK}/week)\n"
    )
    if non_compliant:
        shown = non_compliant[:15]
        text += "\n<b>Non-compliant:</b>\n" + "\n".join(shown)
        if len(non_compliant) > len(shown):
            text += f"\n…and {len(non_compliant) - len(shown)} more"
    else:
        text += "\n✅ All registered drivers are compliant."
    return text, _back_kb("adm:menu", "« Menu")


async def _render_admins() -> tuple[str, InlineKeyboardMarkup]:
    admins = await get_admins()
    kb = InlineKeyboardMarkup(row_width=1)
    lines = ["<b>🛡 Admins</b>\n"]
    if admins:
        for a in admins:
            tag = "★ super-admin" if a.get("is_super_admin") else "admin"
            lines.append(f"• <code>{a['user_id']}</code> — {tag}")
            if not a.get("is_super_admin"):
                kb.add(InlineKeyboardButton(
                    f"❌ Remove {a['user_id']}", callback_data=f"adm:rmadmin:{a['user_id']}"
                ))
    else:
        lines.append("No admins yet.")
    lines.append(
        "\nRegular admins can open this panel and disable a group's notifications. "
        "Only super-admins manage admins."
    )
    kb.add(InlineKeyboardButton("➕ Add admin", callback_data="adm:addadmin"))
    kb.add(InlineKeyboardButton("« Menu", callback_data="adm:menu"))
    return "\n".join(lines), kb


# Short, admin-facing hint shown next to each model id. Keep the actual id as the
# stored/selected value; these are just to remind which is which at a glance.
_MODEL_HINTS = {
    "gemini-2.5-pro": "2.5 — most accurate, slowest",
    "gemini-2.5-flash": "2.5 — faster, cheaper",
    "gemini-2.5-flash-lite": "2.5 — fastest, cheapest",
    "gemini-3-pro-preview": "3.0 Pro (preview)",
    "gemini-3-flash-preview": "3.0 Flash (preview)",
    "gemini-3.1-pro-preview": "3.1 Pro (preview)",
    "gemini-3.1-flash-lite": "3.1 Flash-Lite",
    "gemini-3.5-flash": "3.5 Flash — newest, lots of capacity",
}


async def _render_model(is_super: bool) -> tuple[str, InlineKeyboardMarkup]:
    active = get_active_model()
    kb = InlineKeyboardMarkup(row_width=1)
    if is_super:
        for model in AVAILABLE_GEMINI_MODELS:
            mark = "✅ " if model == active else ""
            kb.add(InlineKeyboardButton(f"{mark}{model}", callback_data=f"adm:setmodel:{model}"))
    kb.add(InlineKeyboardButton("« Menu", callback_data="adm:menu"))

    lines = ["<b>🤖 AI Model</b>\n", f"Active: <code>{escape(active)}</code>\n"]
    for model in AVAILABLE_GEMINI_MODELS:
        hint = _MODEL_HINTS.get(model, "")
        lines.append(f"• <code>{escape(model)}</code>" + (f" — {hint}" if hint else ""))
    lines.append(
        "\nTap a model to switch. Applies to the next inspection." if is_super
        else "\nOnly super-admins can switch the model."
    )
    return "\n".join(lines), kb


# ---------- command entrypoints ----------

@dp.message_handler(commands=["admin"], chat_type=PRIVATE, state="*")
async def cmd_admin(message: types.Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        return  # stay silent so the panel isn't discoverable by non-admins
    await state.finish()
    await message.answer(MENU_TEXT, reply_markup=_menu_kb(await _is_super(message.from_user.id)))


@dp.message_handler(commands=["cancel"], chat_type=PRIVATE, state="*")
async def cmd_cancel(message: types.Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        return
    if await state.get_state() is None:
        return
    await state.finish()
    await message.answer("Cancelled.", reply_markup=_menu_kb(await _is_super(message.from_user.id)))


# ---------- callback router ----------

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("adm:"), state="*")
async def admin_cb(query: types.CallbackQuery, state: FSMContext):
    if not await _is_admin(query.from_user.id):
        await query.answer("Not authorized.", show_alert=True)
        return

    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    # Any navigation cancels a pending text-input flow; broadcast send keeps state.
    if action != "bcsend":
        await state.finish()

    if action == "menu":
        await _edit(query, MENU_TEXT, _menu_kb(await _is_super(query.from_user.id)))
        return

    if action == "notif":
        gid = int(parts[2])
        g = await get_group(gid)
        new_disabled = not (g.get("notifications_disabled") if g else False)
        await set_group_notifications(gid, new_disabled)
        await query.answer("Notifications disabled." if new_disabled else "Notifications enabled.")
        text, kb = await _render_group(gid)
        await _edit(query, text, kb)
        return

    if action in ("admins", "addadmin", "rmadmin"):
        if not await _is_super(query.from_user.id):
            await query.answer("Super-admins only.", show_alert=True)
            return
        if action == "rmadmin":
            await remove_admin(int(parts[2]))
            await query.answer("Admin removed.")
        elif action == "addadmin":
            await AdminSG.add_admin_id.set()
            await _edit(
                query,
                "Send the numeric Telegram user id to add as a regular admin.\n\n"
                "Tip: forward a message from them to @userinfobot to get their id. "
                "/cancel to abort.",
                _back_kb("adm:admins", "« Cancel"),
            )
            return
        text, kb = await _render_admins()
        await _edit(query, text, kb)
        return

    if action == "close":
        try:
            await query.message.delete()
        except Exception:
            await _edit(query, "Panel closed. Send /admin to reopen.", None)
        await query.answer()
        return

    if action == "groups":
        page = int(parts[2]) if len(parts) > 2 else 0
        text, kb = await _render_groups(page)
        await _edit(query, text, kb)
        return

    if action == "g":
        text, kb = await _render_group(int(parts[2]))
        await _edit(query, text, kb)
        return

    if action == "dr":
        text, kb = await _render_drivers(int(parts[2]))
        await _edit(query, text, kb)
        return

    if action == "drm":
        gid, uid = int(parts[2]), int(parts[3])
        await remove_driver(gid, uid)
        await query.answer("Driver removed.")
        text, kb = await _render_drivers(gid)
        await _edit(query, text, kb)
        return

    if action == "tog":
        gid = int(parts[2])
        g = await get_group(gid)
        new_active = not (g.get("is_active", True) if g else True)
        await set_group_active(gid, new_active)
        await query.answer("Reactivated." if new_active else "Deactivated.")
        text, kb = await _render_group(gid)
        await _edit(query, text, kb)
        return

    if action == "plog":
        text, kb = await _render_pti_log(int(parts[2]))
        await _edit(query, text, kb)
        return

    if action == "stats":
        await query.answer("Computing…")
        text, kb = await _render_stats()
        await _edit(query, text, kb)
        return

    if action == "model":
        text, kb = await _render_model(await _is_super(query.from_user.id))
        await _edit(query, text, kb)
        return

    if action == "setmodel":
        # Re-check here too — the buttons only render for super-admins, but a
        # stale keyboard (or a demoted admin) could still fire the callback.
        if not await _is_super(query.from_user.id):
            await query.answer("Super-admins only.", show_alert=True)
            return
        model = parts[2] if len(parts) > 2 else ""
        if not set_active_model(model):
            await query.answer("Unknown model.", show_alert=True)
            return
        await set_setting("gemini_model", model)  # persist across restarts
        await query.answer(f"Model set to {model}.")
        logging.info("Admin %s switched Gemini model to %s", query.from_user.id, model)
        text, kb = await _render_model(True)  # only reachable by super-admins
        await _edit(query, text, kb)
        return

    if action == "su":
        gid = int(parts[2])
        await AdminSG.set_unit.set()
        await state.update_data(gid=gid)
        await _edit(
            query,
            "Send the new unit number for this group as a message.\n\n"
            "It takes effect immediately (no 3-vote). /cancel to abort.",
            _back_kb(f"adm:g:{gid}", "« Cancel"),
        )
        return

    if action == "bcast":
        await AdminSG.broadcast.set()
        targets = await get_active_group_ids()
        await _edit(
            query,
            f"Send the message to broadcast to <b>{len(targets)}</b> active groups.\n\n"
            "Formatting is sent as plain text. /cancel to abort.",
            _back_kb("adm:menu", "« Cancel"),
        )
        return

    if action == "bcsend":
        data = await state.get_data()
        text = data.get("bc_text")
        await state.finish()
        if not text:
            await _edit(query, "Nothing to send.", _menu_kb())
            return
        await query.answer("Sending…")
        sent = failed = 0
        for gid in await get_active_group_ids():
            try:
                await bot.send_message(gid, escape(text))
                sent += 1
            except Exception:
                failed += 1
                logging.exception("broadcast to %s failed", gid)
        await _edit(query, f"📣 Broadcast done.\nSent: {sent}\nFailed: {failed}", _menu_kb())
        return

    await query.answer()


# ---------- text-input flows ----------

@dp.message_handler(state=AdminSG.set_unit, chat_type=PRIVATE)
async def admin_set_unit_input(message: types.Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        return
    unit = (message.text or "").strip()
    data = await state.get_data()
    gid = data.get("gid")
    await state.finish()
    if not unit:
        await message.answer("Empty unit — cancelled.", reply_markup=_menu_kb())
        return
    await set_group_unit(int(gid), unit)
    await message.answer(f"✅ Unit set to <b>{escape(unit)}</b>.")
    text, kb = await _render_group(int(gid))
    await message.answer(text, reply_markup=kb)


@dp.message_handler(state=AdminSG.broadcast, chat_type=PRIVATE, content_types=ContentType.TEXT)
async def admin_broadcast_input(message: types.Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Empty message — send some text or /cancel.")
        return
    await state.update_data(bc_text=text)
    targets = await get_active_group_ids()
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton(f"📣 Send to {len(targets)} groups", callback_data="adm:bcsend"))
    kb.add(InlineKeyboardButton("« Cancel", callback_data="adm:menu"))
    await message.answer(
        f"<b>Preview</b>\n\n{escape(text)}\n\nSend to <b>{len(targets)}</b> active groups?",
        reply_markup=kb,
    )


@dp.message_handler(state=AdminSG.add_admin_id, chat_type=PRIVATE)
async def admin_add_admin_input(message: types.Message, state: FSMContext):
    if not await _is_super(message.from_user.id):
        await state.finish()
        return
    raw = (message.text or "").strip()
    await state.finish()
    try:
        uid = int(raw)
    except ValueError:
        await message.answer("That's not a numeric user id. Cancelled.", reply_markup=_menu_kb(True))
        return
    await add_admin(uid, is_super_admin=False)
    await message.answer(f"✅ Added <code>{escape(str(uid))}</code> as a regular admin.")
    text, kb = await _render_admins()
    await message.answer(text, reply_markup=kb)
