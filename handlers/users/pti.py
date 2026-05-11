from aiogram import types
from aiogram.types import ContentType

from loader import dp

_REDIRECT = "PTI inspection is only available in your driver group. Please send your video or photos there."


@dp.message_handler(commands=["check"], chat_type=types.ChatType.PRIVATE)
async def handle_check_private(message: types.Message):
    await message.answer(_REDIRECT)


@dp.message_handler(content_types=[ContentType.PHOTO], chat_type=types.ChatType.PRIVATE)
async def handle_photo_private(message: types.Message):
    await message.answer(_REDIRECT)


@dp.message_handler(
    content_types=[ContentType.VIDEO, ContentType.VIDEO_NOTE, ContentType.DOCUMENT],
    chat_type=types.ChatType.PRIVATE,
)
async def handle_video_private(message: types.Message):
    await message.answer(_REDIRECT)
