from aiogram import types


async def set_default_commands(dp):
    await dp.bot.set_my_commands(
        [
            types.BotCommand("start", "Start the bot"),
            types.BotCommand("help", "Help"),
            types.BotCommand("check", "Reply to a video to run PTI inspection"),
        ]
    )
