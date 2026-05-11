import asyncio
import json
import logging

from aiogram import types
from aiogram.types import ContentType

from loader import dp
from utils.db import (
    get_group, get_drivers, is_registered_driver,
    log_pti, get_cached_check, get_recent_ptis,
)
from utils.pti_processor import process_video, process_photos, process_image_docs
from utils.enforcement import handle_pti_passed
from handlers.groups.monitoring import buffer_message

MEDIA_GROUP_TIMEOUT = 0.8

_media_group_photos: dict[str, list[types.PhotoSize]] = {}
_media_group_reply: dict[str, types.Message] = {}
_media_group_tasks: dict[str, asyncio.Task] = {}

_media_group_docs: dict[str, list[types.Document]] = {}
_media_group_doc_reply: dict[str, types.Message] = {}
_media_group_doc_tasks: dict[str, asyncio.Task] = {}

GROUP_TYPES = [types.ChatType.GROUP, types.ChatType.SUPERGROUP]


async def _group_ready(message: types.Message) -> bool:
    group = await get_group(message.chat.id)
    if not group or not group["setup_complete"]:
        return False
    return True


async def _handle_pti_result(
    message: types.Message,
    text: str | None,
    data: dict | None,
    replied_message_id: int | None = None,
):
    if not text or not data:
        return
    passed = data.get("status") == "PASS"
    await log_pti(
        group_id=message.chat.id,
        user_id=message.from_user.id,
        passed=passed,
        severity=data.get("severity", ""),
        unit_number=data.get("vehicle", {}).get("unit_number"),
        plate=data.get("vehicle", {}).get("plate"),
        result_json=json.dumps(data),
        result_text=text,
        replied_message_id=replied_message_id,
    )
    if passed:
        drivers = await get_drivers(message.chat.id)
        driver = next((d for d in drivers if d["user_id"] == message.from_user.id), None)
        name = driver["name"] if driver else message.from_user.full_name
        await handle_pti_passed(message.chat.id, message.from_user.id, name)


# ---------- /check ----------

@dp.message_handler(commands=["check"], chat_type=GROUP_TYPES)
async def handle_check_group(message: types.Message):
    if not await _group_ready(message):
        await message.answer(
            "This group is not configured yet. An admin must:\n"
            "1. Register a driver: <code>/adddriver @username Name</code>\n"
            "2. Set the unit number: <code>/setunit &lt;unit_number&gt;</code>",
            parse_mode="HTML",
        )
        return

    reply = message.reply_to_message
    if not reply:
        await message.answer("Reply to a video or photo with /check.")
        return

    cached = await get_cached_check(message.chat.id, reply.message_id)
    if cached:
        await message.reply(cached, parse_mode="HTML")
        return

    history = await get_recent_ptis(message.chat.id, limit=5)
    text, data = None, None

    if reply.photo:
        text, data = await process_photos([reply.photo[-1]], message, history=history)
    elif reply.document and (reply.document.mime_type or "").startswith("image/"):
        text, data = await process_image_docs([reply.document], message, history=history)
    elif reply.video or reply.video_note:
        file = reply.video or reply.video_note
        text, data = await process_video(file, message, history=history)
    elif reply.document and (reply.document.mime_type or "").startswith("video/"):
        text, data = await process_video(reply.document, message, history=history)
    else:
        await message.answer("The replied message is not a video or photo.")
        return

    await _handle_pti_result(message, text, data, replied_message_id=reply.message_id)


# ---------- auto PTI from registered drivers ----------

@dp.message_handler(content_types=[ContentType.PHOTO], chat_type=GROUP_TYPES)
async def handle_group_photo(message: types.Message):
    buffer_message(message)
    if not await _group_ready(message):
        return
    if not await is_registered_driver(message.chat.id, message.from_user.id):
        return

    history = await get_recent_ptis(message.chat.id, limit=5)
    media_group_id = message.media_group_id

    if not media_group_id:
        text, data = await process_photos([message.photo[-1]], message, history=history)
        await _handle_pti_result(message, text, data)
        return

    _media_group_photos.setdefault(media_group_id, []).append(message.photo[-1])
    if media_group_id not in _media_group_reply:
        _media_group_reply[media_group_id] = message
    if media_group_id in _media_group_tasks:
        _media_group_tasks[media_group_id].cancel()

    async def _flush(mgid: str):
        await asyncio.sleep(MEDIA_GROUP_TIMEOUT)
        photos = _media_group_photos.pop(mgid, [])
        reply_to = _media_group_reply.pop(mgid, None)
        _media_group_tasks.pop(mgid, None)
        if photos and reply_to:
            h = await get_recent_ptis(reply_to.chat.id, limit=5)
            text, data = await process_photos(photos, reply_to, history=h)
            await _handle_pti_result(reply_to, text, data)

    _media_group_tasks[media_group_id] = asyncio.create_task(_flush(media_group_id))


@dp.message_handler(
    content_types=[ContentType.VIDEO, ContentType.VIDEO_NOTE, ContentType.DOCUMENT],
    chat_type=GROUP_TYPES,
)
async def handle_group_video(message: types.Message):
    buffer_message(message)
    if not await _group_ready(message):
        return
    if not await is_registered_driver(message.chat.id, message.from_user.id):
        return

    history = await get_recent_ptis(message.chat.id, limit=5)

    if message.document and (message.document.mime_type or "").startswith("image/"):
        media_group_id = message.media_group_id
        if not media_group_id:
            text, data = await process_image_docs([message.document], message, history=history)
            await _handle_pti_result(message, text, data)
            return

        _media_group_docs.setdefault(media_group_id, []).append(message.document)
        if media_group_id not in _media_group_doc_reply:
            _media_group_doc_reply[media_group_id] = message
        if media_group_id in _media_group_doc_tasks:
            _media_group_doc_tasks[media_group_id].cancel()

        async def _flush_docs(mgid: str):
            await asyncio.sleep(MEDIA_GROUP_TIMEOUT)
            docs = _media_group_docs.pop(mgid, [])
            reply_to = _media_group_doc_reply.pop(mgid, None)
            _media_group_doc_tasks.pop(mgid, None)
            if docs and reply_to:
                h = await get_recent_ptis(reply_to.chat.id, limit=5)
                text, data = await process_image_docs(docs, reply_to, history=h)
                await _handle_pti_result(reply_to, text, data)

        _media_group_doc_tasks[media_group_id] = asyncio.create_task(_flush_docs(media_group_id))
        return

    if message.document and not (message.document.mime_type or "").startswith("video/"):
        return

    file = message.video or message.video_note or message.document
    if file:
        text, data = await process_video(file, message, history=history)
        await _handle_pti_result(message, text, data)
