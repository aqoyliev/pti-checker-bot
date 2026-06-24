from __future__ import annotations

import hashlib
import html
import json
import logging

from aiogram import types
from aiogram.types import ContentType

from loader import dp
from utils.db import (
    get_group, get_drivers, is_registered_driver,
    log_pti, get_cached_check, get_recent_ptis,
    set_truck_plate, set_trailer, set_truck_unit, find_open_vehicle_change,
    reset_group_reminders,
)
from utils.pti_processor import process_mixed_media
from utils.enforcement import handle_pti_passed
from handlers.groups.monitoring import buffer_message, get_album_media

GROUP_TYPES = [types.ChatType.GROUP, types.ChatType.SUPERGROUP]

# When True, a detected TRUCK change holds the PTI for a 3-member vote before the
# unit is updated (#5). A TRAILER change never votes — it is applied immediately
# (see _reconcile_vehicles).
TRUCK_CHANGE_CONFIRMATION = True

# When True, /check accepts media from ANYONE and attributes the PTI to whoever
# sent it. When False (current), /check only runs on a registered driver's media
# and otherwise replies that the video isn't from a registered driver. Either
# way, anyone may *type* /check — the flag only governs whose media is accepted.
# The *automatic* standalone-video inspection is always restricted to registered
# drivers regardless of this flag (#4) — see handle_group_video.
ALLOW_ALL_MEMBERS = False

# When True, PTI runs are persisted via log_pti and the dedup cache is active.
# Required for the reminder engine (#8/#9) to know who submitted when, and for
# quotas/history. (Vehicle reconciliation runs regardless.)
SAVE_PTI_LOGS = True

# Hardcoded TEST groups: the bot behaves "as before" in these — it auto-inspects
# EVERYONE's video (not just registered drivers) and skips the recycled-video
# dedup so the same clip can be re-sent while testing.
TEST_GROUP_IDS = {-1004376739828, -1003755811659}


async def _group_ready(message: types.Message) -> bool:
    group = await get_group(message.chat.id)

    if not group or not group["setup_complete"]:
        return False
    return True


def _items_from_reply(reply: types.Message) -> list[dict] | None:
    if reply.photo:
        return [{"kind": "photo", "obj": reply.photo[-1]}]
    if reply.document and (reply.document.mime_type or "").startswith("image/"):
        return [{"kind": "image_doc", "obj": reply.document}]
    if reply.video:
        return [{"kind": "video", "obj": reply.video}]
    if reply.video_note:
        return [{"kind": "video_note", "obj": reply.video_note}]
    if reply.document and (reply.document.mime_type or "").startswith("video/"):
        return [{"kind": "video_doc", "obj": reply.document}]
    return None


def _items_from_buffered(buf_item) -> dict | None:
    if buf_item.content_type == "photo" and buf_item.photo_size:
        return {"kind": "photo", "obj": buf_item.photo_size}
    if buf_item.content_type == "video" and buf_item.video:
        return {"kind": "video", "obj": buf_item.video}
    if buf_item.content_type == "video_note" and buf_item.video_note:
        return {"kind": "video_note", "obj": buf_item.video_note}
    if buf_item.content_type == "document" and buf_item.document:
        mime = (buf_item.mime_type or "")
        if mime.startswith("image/"):
            return {"kind": "image_doc", "obj": buf_item.document}
        if mime.startswith("video/"):
            return {"kind": "video_doc", "obj": buf_item.document}
    return None


def _signature_from_items(items: list[dict]) -> str | None:
    """Return a dedup signature for this submission.

    Hashes the sorted Telegram ``file_unique_id``s of every photo/video item.
    ``file_unique_id`` is globally stable per-file, so the same set of media —
    whether a single video, a photo album, or mixed — collapses to the same
    signature even across forwards and re-uploads by the same user.
    """
    uids: list[str] = []
    for it in items:
        obj = it["obj"]
        uid = getattr(obj, "file_unique_id", None)
        if uid:
            uids.append(uid)
    if not uids:
        return None
    uids.sort()
    h = hashlib.sha1("|".join(uids).encode()).hexdigest()[:20]
    return f"album:{h}"


_VIDEO_KINDS = ("video", "video_note", "video_doc")


def _content_signature_from_items(items: list[dict]) -> str | None:
    """Second dedup signature for video items, based on ``(file_size, duration)``.

    Telegram assigns a fresh ``file_unique_id`` on every upload session, so
    re-uploading the same video defeats the file_unique_id signature. Size and
    duration stay identical for a byte-for-byte re-upload, so this catches that
    case. Photos are excluded because Telegram re-compresses them client-side,
    making size unreliable. Returns None if no video items have usable metadata.
    """
    parts: list[tuple[int, int]] = []
    for it in items:
        if it["kind"] not in _VIDEO_KINDS:
            continue
        obj = it["obj"]
        size = getattr(obj, "file_size", None)
        duration = getattr(obj, "duration", 0) or 0
        if size:
            parts.append((size, duration))
    if not parts:
        return None
    parts.sort()
    h = hashlib.sha1(repr(parts).encode()).hexdigest()[:20]
    return f"content:{h}"


def _extract_vehicles(data: dict) -> list[dict]:
    """Normalize Gemini's vehicle output. Returns a list of {type, unit_number, plate}."""
    vehicles = data.get("vehicles")
    if isinstance(vehicles, list):
        out = [v for v in vehicles if isinstance(v, dict)]
    else:
        v = data.get("vehicle")
        out = [v] if isinstance(v, dict) else []
    norm: list[dict] = []
    for v in out:
        vtype = (v.get("type") or "").lower()
        if vtype not in ("truck", "trailer"):
            continue
        unit = v.get("unit_number")
        plate = v.get("plate")
        norm.append({"type": vtype, "unit_number": unit, "plate": plate})
    return norm


def _truck_log_fields(vehicles: list[dict]) -> tuple[str | None, str | None]:
    truck = next((v for v in vehicles if v["type"] == "truck"), None)
    trailer = next((v for v in vehicles if v["type"] == "trailer"), None)
    primary = truck or trailer
    if not primary:
        return None, None
    return primary.get("unit_number"), primary.get("plate")


def _truck_change_suspected(group: dict | None, data: dict) -> tuple[str, str | None] | None:
    """If Gemini reports a truck unit that differs from the registered one, return
    (new_unit, new_plate). Otherwise None.
    """
    if not group or not group.get("unit_number"):
        return None
    for v in _extract_vehicles(data):
        if v["type"] != "truck":
            continue
        new_unit = (v.get("unit_number") or "").strip()
        if new_unit and new_unit != group["unit_number"]:
            new_plate = (v.get("plate") or "").strip() or None
            return new_unit, new_plate
    return None


async def _notify_vehicle_change(
    message: types.Message,
    old_truck_unit: str | None,
    new_truck_unit: str,
    new_truck_plate: str | None,
    trailer_unit: str | None,
    trailer_plate: str | None,
):
    """Post a short notice that the PTI is for a different truck than the
    registered one, including the new truck and trailer unit/plate numbers.
    """
    old = html.escape(old_truck_unit) if old_truck_unit else "—"
    truck_line = f"🚚 Truck: <b>{old} → {html.escape(new_truck_unit)}</b>"
    if new_truck_plate:
        truck_line += f" (plate {html.escape(new_truck_plate)})"

    lines = ["🔁 <b>Vehicle change detected</b>", truck_line]
    if trailer_unit or trailer_plate:
        trailer_line = "🚛 Trailer:"
        if trailer_unit:
            trailer_line += f" unit <b>{html.escape(trailer_unit)}</b>"
        if trailer_plate:
            trailer_line += f" plate {html.escape(trailer_plate)}"
        lines.append(trailer_line)

    try:
        await message.answer("\n".join(lines), parse_mode="HTML")
    except Exception:
        logging.exception("Failed to send vehicle-change notice")


async def _reconcile_vehicles(
    message: types.Message,
    pti_log_id: int,
    data: dict,
    result_message_id: int | None,
):
    from handlers.groups.proposals import propose_vehicle_change

    vehicles = _extract_vehicles(data)
    if not vehicles:
        return

    group = await get_group(message.chat.id)
    if not group:
        return

    truck = next((v for v in vehicles if v["type"] == "truck"), None)
    trailer = next((v for v in vehicles if v["type"] == "trailer"), None)

    def _norm(v: dict | None) -> tuple[str | None, str | None]:
        if not v:
            return None, None
        u = (v.get("unit_number") or "").strip() or None
        p = (v.get("plate") or "").strip() or None
        return u, p

    truck_unit, truck_plate = _norm(truck)
    trailer_unit, trailer_plate = _norm(trailer)

    stored_truck_unit = group.get("unit_number")
    stored_truck_plate = group.get("truck_plate")
    stored_trailer_unit = group.get("trailer_unit")
    stored_trailer_plate = group.get("trailer_plate")

    truck_changed = bool(
        stored_truck_unit and truck_unit and truck_unit != stored_truck_unit
    )

    # #5: a TRAILER change never goes to a vote — apply it immediately, whether or
    # not the truck also changed.
    if trailer:
        if trailer_unit and trailer_unit != stored_trailer_unit:
            await set_trailer(message.chat.id, trailer_unit, trailer_plate)
        elif not stored_trailer_unit and trailer_unit:
            await set_trailer(message.chat.id, trailer_unit, trailer_plate)
        elif trailer_plate and trailer_plate != stored_trailer_plate:
            await set_trailer(message.chat.id, None, trailer_plate)

    if truck_changed:
        if not TRUCK_CHANGE_CONFIRMATION:
            # Adopt the new truck silently.
            await set_truck_unit(message.chat.id, truck_unit, truck_plate)
            await _notify_vehicle_change(
                message, stored_truck_unit, truck_unit, truck_plate,
                trailer_unit, trailer_plate,
            )
            return
        # #5: hold the PTI for a 3-member vote on the TRUCK change only. The
        # trailer was already applied above, so nothing is bundled into the vote.
        if await find_open_vehicle_change(message.chat.id, "truck"):
            return
        await propose_vehicle_change(
            chat_id=message.chat.id,
            kind="truck",
            current_unit=stored_truck_unit,
            new_unit=truck_unit,
            new_plate=truck_plate,
            pti_log_id=pti_log_id,
            result_message_id=result_message_id,
            bundled_trailer=None,
        )
        return

    # No truck change → reconcile the truck plate silently.
    if truck:
        if not stored_truck_unit and truck_plate and not stored_truck_plate:
            await set_truck_plate(message.chat.id, truck_plate)
        elif truck_plate and truck_plate != stored_truck_plate:
            await set_truck_plate(message.chat.id, truck_plate)


async def _handle_pti_result(
    message: types.Message,
    text: str | None,
    data: dict | None,
    driver_user_id: int,
    driver_name: str | None,
    replied_message_id: int | None = None,
    media_signature: str | None = None,
    content_signature: str | None = None,
    result_message_id: int | None = None,
    truck_change_pending: bool = False,
):
    if not text or not data:
        return
    passed = data.get("status") == "PASS"
    vehicles = _extract_vehicles(data)
    primary_unit, primary_plate = _truck_log_fields(vehicles)
    pti_log_id = None
    if SAVE_PTI_LOGS:
        pti_log_id = await log_pti(
            group_id=message.chat.id,
            user_id=driver_user_id,
            passed=passed,
            severity=data.get("severity", ""),
            unit_number=primary_unit,
            plate=primary_plate,
            result_json=json.dumps(data),
            result_text=text,
            replied_message_id=replied_message_id,
            media_signature=media_signature,
            driver_name=driver_name,
            content_signature=content_signature,
        )
    await _reconcile_vehicles(message, pti_log_id, data, result_message_id)
    # A driver submitting any PTI clears the overdue/escalation reminders (#9).
    await reset_group_reminders(message.chat.id)
    if not truck_change_pending:
        await handle_pti_passed(message.chat.id, driver_user_id, driver_name or str(driver_user_id))


# ---------- /check ----------

@dp.message_handler(commands=["check"], chat_type=GROUP_TYPES)
async def handle_check_group(message: types.Message):
    if not await _group_ready(message):
        await message.answer(
            "This group is not configured yet. Anyone in the group can run:\n"
            "1. Have the driver send a message, then reply with: <code>/adddriver Driver Name</code>\n"
            "2. Set the unit number: <code>/setunit &lt;unit_number&gt;</code>",
            parse_mode="HTML",
        )
        return

    reply = message.reply_to_message
    if not reply:
        await message.answer("Reply to a video or photo with /check.")
        return

    direct_uid = reply.from_user.id if reply.from_user else None
    forward_uid = reply.forward_from.id if reply.forward_from else None
    driver_uid: int | None = None
    if direct_uid and await is_registered_driver(message.chat.id, direct_uid):
        driver_uid = direct_uid
    elif forward_uid and await is_registered_driver(message.chat.id, forward_uid):
        driver_uid = forward_uid
    if driver_uid is None and ALLOW_ALL_MEMBERS:
        # Testing: attribute the PTI to whoever sent the replied media.
        driver_uid = direct_uid or forward_uid
    if driver_uid is None:
        await message.answer(
            "⚠️ This video isn't from a registered driver, so it can't be checked.\n"
            "Reply <code>/check</code> to a <b>registered driver's</b> video, or add the "
            "driver first with <code>/adddriver Driver Name</code>.",
            parse_mode="HTML",
        )
        return

    drivers = await get_drivers(message.chat.id)
    driver_row = next((d for d in drivers if d["user_id"] == driver_uid), None)
    driver_name = driver_row["name"] if driver_row else None
    if not driver_name and ALLOW_ALL_MEMBERS:
        src = reply.forward_from if forward_uid == driver_uid else reply.from_user
        driver_name = src.full_name if src else None

    await _run_pti(message, reply, driver_uid, driver_name)


async def _run_pti(
    message: types.Message,
    reply: types.Message,
    driver_uid: int,
    driver_name: str | None,
):
    """Run the PTI pipeline for the media in ``reply`` and post the result.

    Shared by the ``/check`` command and the standalone-video auto-trigger.
    Caller is responsible for resolving ``driver_uid``/``driver_name`` and for
    the group-ready check.
    """
    items = _items_from_reply(reply)
    if items is None:
        await message.answer("The replied message is not a video or photo.")
        return

    for item in items:
        obj = item["obj"]
        duration = getattr(obj, "duration", None)
        if duration and duration > 900:
            await message.answer(
                f"⚠️ Video is {duration // 60} min long — too long for a PTI inspection. "
                "Please send a video under 15 minutes."
            )
            return

    if reply.media_group_id:
        seen_ids = {reply.message_id}
        for buf_item in get_album_media(message.chat.id, reply.media_group_id):
            if buf_item.message_id in seen_ids:
                continue
            seen_ids.add(buf_item.message_id)
            converted = _items_from_buffered(buf_item)
            if converted:
                items.append(converted)

    signature = _signature_from_items(items)
    content_sig = _content_signature_from_items(items)
    if SAVE_PTI_LOGS and message.chat.id not in TEST_GROUP_IDS and (signature or content_sig):
        # Dedup by (file_size, duration) — an old video re-uploaded keeps the same
        # size+length even though Telegram assigns it a fresh file id. A match is
        # rejected here BEFORE any inspection runs, so a recycled PTI never counts
        # toward the quota and never clears the overdue reminders.
        cached = await get_cached_check(message.chat.id, signature, content_sig)
        if cached:
            if cached["user_id"] == driver_uid:
                await message.reply(
                    "♻️ This is the <b>same inspection video you already sent</b> "
                    "(same size and length) — it doesn't count.\n"
                    "Please record and send a <b>new</b> pre-trip inspection.",
                    parse_mode="HTML",
                )
            else:
                drivers = await get_drivers(message.chat.id)
                original = next(
                    (d for d in drivers if d["user_id"] == cached["user_id"]),
                    None,
                )
                original_name = (
                    (original and original["name"])
                    or cached.get("driver_name")
                    or "another driver"
                )
                await message.answer(
                    f"♻️ This video was already submitted by {html.escape(original_name)} — "
                    f"it can't be reused. Please record your own inspection."
                )
            return

    group = await get_group(message.chat.id)
    history = await get_recent_ptis(message.chat.id, limit=5)
    current_unit = group.get("unit_number") if group else None
    if current_unit:
        history = [h for h in history if h.get("unit_number") == current_unit]
    text, data, status_msg = await process_mixed_media(
        items, reply, history=history, driver_name=driver_name,
    )

    if text is None or data is None or status_msg is None:
        return  # error path; process_mixed_media already edited the status message

    truck_change = _truck_change_suspected(group, data) if TRUCK_CHANGE_CONFIRMATION else None

    if truck_change:
        new_unit, _ = truck_change
        try:
            await status_msg.edit_text(
                f"⏳ This PTI mentions truck unit <b>{new_unit}</b>, but the registered truck is "
                f"<b>{group['unit_number']}</b>.\n"
                f"Holding the result until 3 members confirm the vehicle change. "
                f"If rejected, the PTI will be marked failed.",
                parse_mode="HTML",
            )
        except Exception:
            logging.exception("Failed to set hold message on status_msg")
    else:
        try:
            await status_msg.edit_text(text, parse_mode="HTML")
        except Exception:
            logging.exception("Failed to render PTI result")

    await _handle_pti_result(
        message, text, data,
        driver_user_id=driver_uid,
        driver_name=driver_name,
        replied_message_id=reply.message_id,
        media_signature=signature,
        content_signature=content_sig,
        result_message_id=status_msg.message_id,
        truck_change_pending=bool(truck_change),
    )


# ---------- media buffering (for vehicle detection + /check lookup) ----------

@dp.message_handler(content_types=[ContentType.PHOTO], chat_type=GROUP_TYPES)
async def handle_group_photo(message: types.Message):
    buffer_message(message)


# Auto-run a PTI only for a single standalone video — not photos, not albums.
# video_note (round messages) and non-video documents are excluded.
_AUTO_VIDEO_KINDS = ("video", "video_doc")


def _is_forwarded_from_other(message: types.Message, sender_uid: int) -> bool:
    """True if this message was forwarded from someone other than its sender.

    A PTI must be the driver's own fresh recording, so a forwarded clip — e.g. a
    driver forwarding another person's video — should be ignored by the
    auto-inspector. A driver forwarding their *own* earlier message still counts
    as their video and is allowed. When the original sender hid their account or
    it was forwarded from a channel/chat, the origin can't be attributed to the
    driver, so it's treated as "from someone else".
    """
    forwarded = bool(
        message.forward_date
        or message.forward_from
        or message.forward_from_chat
        or message.forward_sender_name
    )
    if not forwarded:
        return False
    if message.forward_from is not None:
        return message.forward_from.id != sender_uid
    return True


@dp.message_handler(
    content_types=[ContentType.VIDEO, ContentType.VIDEO_NOTE, ContentType.DOCUMENT],
    chat_type=GROUP_TYPES,
)
async def handle_group_video(message: types.Message):
    buffer_message(message)

    # Auto-inspect a standalone video sent by a registered driver: no /check
    # needed. Cheap guards first, DB lookups last. Stay silent (buffer only)
    # on anything that isn't an eligible standalone video so random group
    # media never triggers an inspection or an error reply.
    if message.media_group_id:
        return  # part of an album — needs an explicit /check
    items = _items_from_reply(message)
    if not items or items[0]["kind"] not in _AUTO_VIDEO_KINDS:
        return
    if not await _group_ready(message):
        return
    uid = message.from_user.id if message.from_user else None
    if not uid:
        return
    # A forwarded video isn't the driver's own fresh recording — e.g. a driver
    # forwarding someone else's clip. Don't auto-inspect it and don't reply; stay
    # silent (buffer only). A driver forwarding their *own* earlier message still
    # counts. TEST groups keep the old behavior and inspect forwards too.
    if message.chat.id not in TEST_GROUP_IDS and _is_forwarded_from_other(message, uid):
        return
    # #4: the bot only AUTO-checks a registered driver's video. Anyone else must
    # use /check explicitly (#6). This holds even when ALLOW_ALL_MEMBERS is on —
    # except in the hardcoded TEST groups, where any member's video auto-checks.
    if message.chat.id not in TEST_GROUP_IDS and not await is_registered_driver(message.chat.id, uid):
        return

    drivers = await get_drivers(message.chat.id)
    driver_row = next((d for d in drivers if d["user_id"] == uid), None)
    driver_name = driver_row["name"] if driver_row else (
        message.from_user.full_name if message.from_user else None
    )
    await _run_pti(message, message, uid, driver_name)
