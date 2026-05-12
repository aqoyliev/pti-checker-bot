from aiogram import types
from aiogram.dispatcher.filters.builtin import CommandStart

from loader import dp


@dp.message_handler(CommandStart(), chat_type=types.ChatType.PRIVATE)
async def bot_start(message: types.Message):
    await message.answer(
        f"Hello, {message.from_user.full_name}!\n\n"
        "Add me to your driver group to get started.\n"
        "Use <code>/check</code> in the group by replying to a photo or video to run a PTI inspection.",
        parse_mode="HTML",
    )
