from collections import OrderedDict

from aiogram import types
from aiogram.types import ContentType

from loader import dp

_REDIRECT = "PTI inspection is only available in your driver group. Please send your video or photos there."

_seen_media_groups: OrderedDict[str, None] = OrderedDict()
_SEEN_LIMIT = 256


def _already_replied(media_group_id: str | None) -> bool:
    if not media_group_id:
        return False
    if media_group_id in _seen_media_groups:
        return True
    _seen_media_groups[media_group_id] = None
    if len(_seen_media_groups) > _SEEN_LIMIT:
        _seen_media_groups.popitem(last=False)
    return False


@dp.message_handler(commands=["check"], chat_type=types.ChatType.PRIVATE)
async def handle_check_private(message: types.Message):
    await message.answer(_REDIRECT)


@dp.message_handler(content_types=[ContentType.PHOTO], chat_type=types.ChatType.PRIVATE)
async def handle_photo_private(message: types.Message):
    if _already_replied(message.media_group_id):
        return
    await message.answer(_REDIRECT)


@dp.message_handler(
    content_types=[ContentType.VIDEO, ContentType.VIDEO_NOTE, ContentType.DOCUMENT],
    chat_type=types.ChatType.PRIVATE,
)
async def handle_video_private(message: types.Message):
    if _already_replied(message.media_group_id):
        return
    await message.answer(_REDIRECT)
