from __future__ import annotations

import json
import logging

from aiogram import types
from aiogram.types import ContentType

from loader import dp
from utils.db import (
    get_group, get_drivers, is_registered_driver,
    log_pti, get_cached_check, get_recent_ptis, find_duplicate_pti,
)
from utils.pti_processor import process_mixed_media
from utils.enforcement import handle_pti_passed
from handlers.groups.monitoring import buffer_message, get_nearby_media

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


def _video_signature(reply: types.Message) -> str | None:
    if reply.video or reply.video_note:
        f = reply.video or reply.video_note
        if f.file_size is not None and f.duration is not None:
            return f"video:{f.file_size}:{f.duration}"
        return None
    if reply.document and (reply.document.mime_type or "").startswith("video/"):
        if reply.document.file_size is not None:
            return f"videodoc:{reply.document.file_size}"
    return None


async def _handle_pti_result(
    message: types.Message,
    text: str | None,
    data: dict | None,
    driver_user_id: int,
    replied_message_id: int | None = None,
    media_signature: str | None = None,
):
    if not text or not data:
        return
    passed = data.get("status") == "PASS"
    await log_pti(
        group_id=message.chat.id,
        user_id=driver_user_id,
        passed=passed,
        severity=data.get("severity", ""),
        unit_number=data.get("vehicle", {}).get("unit_number"),
        plate=data.get("vehicle", {}).get("plate"),
        result_json=json.dumps(data),
        result_text=text,
        replied_message_id=replied_message_id,
        media_signature=media_signature,
    )
    if passed:
        drivers = await get_drivers(message.chat.id)
        driver = next((d for d in drivers if d["user_id"] == driver_user_id), None)
        name = driver["name"] if driver else str(driver_user_id)
        await handle_pti_passed(message.chat.id, driver_user_id, name)


# ---------- /check ----------

@dp.message_handler(commands=["check"], chat_type=GROUP_TYPES)
async def handle_check_group(message: types.Message):
    if not await _group_ready(message):
        await message.answer(
            "This group is not configured yet. An admin must:\n"
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
    if driver_uid is None:
        await message.answer("That message isn't from a registered driver.")
        return

    cached = await get_cached_check(message.chat.id, reply.message_id)
    if cached:
        await message.reply(cached, parse_mode="HTML")
        return

    signature = _video_signature(reply)
    if signature:
        duplicate = await find_duplicate_pti(message.chat.id, driver_uid, signature)
        if duplicate:
            prior = duplicate["submitted_at"].strftime("%Y-%m-%d %H:%M UTC")
            await message.answer(
                f"This video matches a PTI already submitted by this driver on {prior}. "
                "Please record a new inspection."
            )
            return

    items = _items_from_reply(reply)
    if items is None:
        await message.answer("The replied message is not a video or photo.")
        return

    seen_ids = {reply.message_id}
    for buf_item in get_nearby_media(message.chat.id, driver_uid, reply.message_id, window=20):
        if buf_item.message_id in seen_ids:
            continue
        seen_ids.add(buf_item.message_id)
        converted = _items_from_buffered(buf_item)
        if converted:
            items.append(converted)

    history = await get_recent_ptis(message.chat.id, limit=5)
    text, data = await process_mixed_media(items, message, history=history)

    await _handle_pti_result(
        message, text, data,
        driver_user_id=driver_uid,
        replied_message_id=reply.message_id,
        media_signature=signature,
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
