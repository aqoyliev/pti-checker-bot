from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from aiogram import types
from aiogram.types import ContentType

from loader import dp
from utils.db import get_group, get_drivers, is_registered_driver

BUFFER_SIZE = 20
WATCH_TIMEOUT = 180  # seconds to watch for media after keyword

VEHICLE_KEYWORDS = [
    "picked up", "pick up", "pickup",
    "dropped", "drop off", "dropoff",
    "new truck", "new trailer",
    "switched truck", "switched trailer",
    "changed truck", "changed trailer",
    "got the truck", "got the trailer",
    "got a truck", "got a trailer",
    "taking truck", "taking trailer",
    "assigned truck", "assigned trailer",
    "starting truck", "starting trailer",
    "hooked up", "hook up",
]

GROUP_TYPES = [types.ChatType.GROUP, types.ChatType.SUPERGROUP]


@dataclass
class BufferedMessage:
    message_id: int
    user_id: int
    content_type: str
    file_id: str | None
    mime_type: str | None
    timestamp: datetime
    photo_size: types.PhotoSize | None = None
    document: types.Document | None = None
    video: types.Video | None = None
    video_note: types.VideoNote | None = None


@dataclass
class VehicleWatch:
    user_id: int
    trigger_message_id: int
    expires_at: datetime
    task: asyncio.Task | None = field(default=None, repr=False)


_buffers: dict[int, deque[BufferedMessage]] = {}
_watches: dict[int, VehicleWatch] = {}  # group_id → active watch


def _get_buffer(group_id: int) -> deque[BufferedMessage]:
    if group_id not in _buffers:
        _buffers[group_id] = deque(maxlen=BUFFER_SIZE)
    return _buffers[group_id]


def _has_keyword(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in VEHICLE_KEYWORDS)


def get_nearby_media(group_id: int, user_id: int, anchor_message_id: int, window: int = 10) -> list[BufferedMessage]:
    """Return buffered photo/video messages from user_id within ±window of anchor_message_id."""
    buf = _get_buffer(group_id)
    media = []
    for msg in buf:
        if msg.user_id != user_id:
            continue
        if abs(msg.message_id - anchor_message_id) > window:
            continue
        if msg.content_type in ("photo", "video", "video_note", "document"):
            media.append(msg)
    return media


def _effective_sender_id(message: types.Message) -> int:
    """The original author if the message was forwarded, else the direct sender."""
    if message.forward_from:
        return message.forward_from.id
    return message.from_user.id


def buffer_message(message: types.Message) -> None:
    """Buffer a message for vehicle-change detection. Call from any handler that intercepts media."""
    buf = _get_buffer(message.chat.id)
    uid = _effective_sender_id(message)

    if message.photo:
        photo_size = message.photo[-1]
        buf.append(BufferedMessage(
            message_id=message.message_id,
            user_id=uid,
            content_type="photo",
            file_id=photo_size.file_id,
            mime_type=None,
            timestamp=datetime.now(),
            photo_size=photo_size,
        ))
    elif message.video:
        buf.append(BufferedMessage(
            message_id=message.message_id,
            user_id=uid,
            content_type="video",
            file_id=message.video.file_id,
            mime_type=message.video.mime_type,
            timestamp=datetime.now(),
            video=message.video,
        ))
    elif message.video_note:
        buf.append(BufferedMessage(
            message_id=message.message_id,
            user_id=uid,
            content_type="video_note",
            file_id=message.video_note.file_id,
            mime_type=None,
            timestamp=datetime.now(),
            video_note=message.video_note,
        ))
    elif message.document:
        buf.append(BufferedMessage(
            message_id=message.message_id,
            user_id=uid,
            content_type="document",
            file_id=message.document.file_id,
            mime_type=message.document.mime_type,
            timestamp=datetime.now(),
            document=message.document,
        ))


@dp.message_handler(
    lambda m: not (m.text or "").startswith("/"),
    content_types=[ContentType.TEXT],
    chat_type=GROUP_TYPES,
)
async def monitor_group_messages(message: types.Message):
    group = await get_group(message.chat.id)
    if not group or not group["setup_complete"]:
        return

    text = message.text or message.caption or ""
    if not text or not _has_keyword(text):
        return

    drivers = await get_drivers(message.chat.id)
    driver_ids = {d["user_id"] for d in drivers}
    if not driver_ids:
        return

    nearby = []
    for driver_id in driver_ids:
        nearby.extend(get_nearby_media(message.chat.id, driver_id, message.message_id))

    if nearby:
        from handlers.groups.vehicle_detection import process_vehicle_media
        await process_vehicle_media(message.chat.id, nearby, message.chat)
        return

    for driver_id in driver_ids:
        watch = _watches.get(message.chat.id)
        if watch and watch.user_id == driver_id:
            continue

        expires_at = datetime.now() + timedelta(seconds=WATCH_TIMEOUT)

        async def _watch(group_id: int, uid: int, chat: types.Chat, exp: datetime):
            try:
                while datetime.now() < exp:
                    await asyncio.sleep(5)
                    media = get_nearby_media(group_id, uid, message.message_id, window=50)
                    if media:
                        from handlers.groups.vehicle_detection import process_vehicle_media
                        await process_vehicle_media(group_id, media, chat)
                        break
            except asyncio.CancelledError:
                pass
            finally:
                if _watches.get(group_id) and _watches[group_id].user_id == uid:
                    _watches.pop(group_id, None)

        if message.chat.id in _watches and _watches[message.chat.id].task:
            _watches[message.chat.id].task.cancel()

        task = asyncio.create_task(_watch(message.chat.id, driver_id, message.chat, expires_at))
        _watches[message.chat.id] = VehicleWatch(
            user_id=driver_id,
            trigger_message_id=message.message_id,
            expires_at=expires_at,
            task=task,
        )
