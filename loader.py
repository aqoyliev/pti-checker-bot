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
