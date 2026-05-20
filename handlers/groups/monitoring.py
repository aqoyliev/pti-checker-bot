from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime

from aiogram import types

BUFFER_SIZE = 20


@dataclass
class BufferedMessage:
    message_id: int
    user_id: int
    content_type: str
    file_id: str | None
    mime_type: str | None
    timestamp: datetime
    media_group_id: str | None = None
    photo_size: types.PhotoSize | None = None
    document: types.Document | None = None
    video: types.Video | None = None
    video_note: types.VideoNote | None = None


_buffers: dict[int, deque[BufferedMessage]] = {}


def _get_buffer(group_id: int) -> deque[BufferedMessage]:
    if group_id not in _buffers:
        _buffers[group_id] = deque(maxlen=BUFFER_SIZE)
    return _buffers[group_id]


def get_album_media(group_id: int, media_group_id: str) -> list[BufferedMessage]:
    """Return all buffered media items belonging to the given Telegram album."""
    buf = _get_buffer(group_id)
    return [
        msg for msg in buf
        if msg.media_group_id == media_group_id
        and msg.content_type in ("photo", "video", "video_note", "document")
    ]


def _effective_sender_id(message: types.Message) -> int:
    """The original author if the message was forwarded, else the direct sender."""
    if message.forward_from:
        return message.forward_from.id
    return message.from_user.id


def buffer_message(message: types.Message) -> None:
    """Buffer a media message so /check can reassemble album siblings."""
    buf = _get_buffer(message.chat.id)
    uid = _effective_sender_id(message)
    mgid = message.media_group_id

    if message.photo:
        photo_size = message.photo[-1]
        buf.append(BufferedMessage(
            message_id=message.message_id,
            user_id=uid,
            content_type="photo",
            file_id=photo_size.file_id,
            mime_type=None,
            timestamp=datetime.now(),
            media_group_id=mgid,
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
            media_group_id=mgid,
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
            media_group_id=mgid,
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
            media_group_id=mgid,
            document=message.document,
        ))
