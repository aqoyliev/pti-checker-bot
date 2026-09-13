from aiogram import Bot, Dispatcher, types
from aiogram.bot.api import TelegramAPIServer
from aiogram.contrib.fsm_storage.memory import MemoryStorage

from data import config

if config.LOCAL_SERVER_URL:
    server = TelegramAPIServer.from_base(config.LOCAL_SERVER_URL)
    bot = Bot(token=config.BOT_TOKEN, parse_mode=types.ParseMode.HTML, server=server)
else:
    bot = Bot(token=config.BOT_TOKEN, parse_mode=types.ParseMode.HTML)

storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

_bot_id: int | None = None


async def bot_id() -> int:
    """This bot's own user id, fetched once and cached.

    Several handlers need it on every update they see -- "is this a reply to
    *our* message", "was it *us* who just got added" -- and a getMe round-trip
    per group event is the wrong price for a number that never changes.
    """
    global _bot_id
    if _bot_id is None:
        _bot_id = (await bot.get_me()).id
    return _bot_id
