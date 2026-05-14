from __future__ import annotations

import hashlib
import json
import logging
from zoneinfo import ZoneInfo

from aiogram import types
from aiogram.types import ContentType

EASTERN = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

from loader import dp
from utils.db import (
    get_group, get_drivers, is_registered_driver,
    log_pti, get_cached_check, get_recent_ptis, find_duplicate_pti,
    set_truck_plate, set_trailer, find_open_vehicle_change,
)
from utils.pti_processor import process_mixed_media
from utils.enforcement import handle_pti_passed
from handlers.groups.monitoring import buffer_message, get_album_media

GROUP_TYPES = [types.ChatType.GROUP, types.ChatType.SUPERGROUP]


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
    trailer_changed = bool(
        trailer_unit and trailer_unit != stored_trailer_unit
    )

    if truck_changed:
        if await find_open_vehicle_change(message.chat.id, "truck"):
            return
        bundled_trailer = None
        if trailer_changed or (not stored_trailer_unit and trailer_unit):
            bundled_trailer = {"unit": trailer_unit, "plate": trailer_plate}
        await propose_vehicle_change(
            chat_id=message.chat.id,
            kind="truck",
            current_unit=stored_truck_unit,
            new_unit=truck_unit,
            new_plate=truck_plate,
            pti_log_id=pti_log_id,
            result_message_id=result_message_id,
            bundled_trailer=bundled_trailer,
        )
        return

    # No truck change → reconcile each side silently.
    if truck and not truck_changed:
        if not stored_truck_unit and truck_plate and not stored_truck_plate:
            await set_truck_plate(message.chat.id, truck_plate)
        elif truck_plate and truck_plate != stored_truck_plate:
            await set_truck_plate(message.chat.id, truck_plate)

    if trailer:
        if trailer_unit and trailer_unit != stored_trailer_unit:
            await set_trailer(message.chat.id, trailer_unit, trailer_plate)
        elif not stored_trailer_unit and trailer_unit:
            await set_trailer(message.chat.id, trailer_unit, trailer_plate)
        elif trailer_plate and trailer_plate != stored_trailer_plate:
            await set_trailer(message.chat.id, None, trailer_plate)


async def _handle_pti_result(
    message: types.Message,
    text: str | None,
    data: dict | None,
    driver_user_id: int,
    driver_name: str | None,
    replied_message_id: int | None = None,
    media_signature: str | None = None,
    result_message_id: int | None = None,
    truck_change_pending: bool = False,
):
    if not text or not data:
        return
    passed = data.get("status") == "PASS"
    vehicles = _extract_vehicles(data)
    primary_unit, primary_plate = _truck_log_fields(vehicles)
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
    )
    await _reconcile_vehicles(message, pti_log_id, data, result_message_id)
    if passed and not truck_change_pending:
        await handle_pti_passed(message.chat.id, driver_user_id, driver_name or str(driver_user_id))


# ---------- /check ----------

@dp.message_handler(commands=["check"], chat_type=GROUP_TYPES)
async def handle_check_group(message: types.Message):
    if not await _group_ready(message):
        await message.answer(
            "This group is not configured yet. Anyone in the group can run:\n"
            "1. Have the driver send a message, then reply with: <code>/adddriver Driver Name</code>\n"
            "2. Set the unit number: <code>/setunit &lt;unit_number&gt;</code>\n\n"
            "Each command needs 3 confirmations from members before it takes effect.",
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
    if driver_uid is None:
        await message.answer("That message isn't from a registered driver.")
        return

    drivers = await get_drivers(message.chat.id)
    driver_row = next((d for d in drivers if d["user_id"] == driver_uid), None)
    driver_name = driver_row["name"] if driver_row else None

    cached = await get_cached_check(message.chat.id, reply.message_id)
    if cached:
        await message.reply(cached, parse_mode="HTML")
        return

    items = _items_from_reply(reply)
    if items is None:
        await message.answer("The replied message is not a video or photo.")
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
    if signature:
        duplicate = await find_duplicate_pti(message.chat.id, driver_uid, signature)
        if duplicate:
            prior_dt = duplicate["submitted_at"].replace(tzinfo=UTC).astimezone(EASTERN)
            prior = prior_dt.strftime("%Y-%m-%d %I:%M %p %Z")
            await message.answer(
                f"This media matches a PTI already submitted by this driver on {prior}. "
                "Please record a new inspection."
            )
            return

    history = await get_recent_ptis(message.chat.id, limit=5)
    text, data, status_msg = await process_mixed_media(
        items, message, history=history, driver_name=driver_name,
    )

    if text is None or data is None or status_msg is None:
        return  # error path; process_mixed_media already edited the status message

    group = await get_group(message.chat.id)
    truck_change = _truck_change_suspected(group, data)

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
        result_message_id=status_msg.message_id,
        truck_change_pending=bool(truck_change),
    )


# ---------- media buffering (for vehicle detection + /check lookup) ----------

@dp.message_handler(content_types=[ContentType.PHOTO], chat_type=GROUP_TYPES)
async def handle_group_photo(message: types.Message):
    buffer_message(message)


@dp.message_handler(
    content_types=[ContentType.VIDEO, ContentType.VIDEO_NOTE, ContentType.DOCUMENT],
    chat_type=GROUP_TYPES,
)
async def handle_group_video(message: types.Message):
    buffer_message(message)
