from aiogram import types


async def set_default_commands(dp):
    await dp.bot.set_my_commands(
        [
            types.BotCommand("start", "Start the bot"),
            types.BotCommand("help", "Help"),
            types.BotCommand("check", "Run PTI inspection on a replied video or photo"),
            types.BotCommand("adddriver", "Register a driver in this group"),
            types.BotCommand("setunit", "Set the current truck unit number"),
        ]
    )
