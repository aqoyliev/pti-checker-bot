from aiogram import types
from aiogram.dispatcher.filters.builtin import CommandHelp

from loader import dp

_INTRO = (
    "🛠️ <b>PTI Checker Bot — Help</b>\n\n"
    "I review pre-trip inspection (PTI) media and decide PASS/FAIL using a DOT-trained model.\n\n"
)

# Setup is a group-only concern -- /setunit and /adddriver only work inside a
# group chat, so telling a DM about them just points at a command that will
# fail there.
_SETUP = (
    "<b>Setup (one-time per group)</b>\n"
    "When I'm added to a group, I read the group name and bio to detect the unit number and "
    "driver names. If anything is missing, anyone in the group can run:\n"
    "• <code>/setunit &lt;unit_number&gt;</code> — set the truck unit\n"
    "• <code>/adddriver Driver Name</code> — reply to the driver's message\n\n"
)

_BODY = (
    "<b>Running an inspection</b>\n"
    "1. The driver sends the PTI as one or more photos and/or a video (one album works best).\n"
    "2. Anyone replies to that media with <code>/check</code>.\n"
    "3. I'll analyze every photo + every sampled video frame together and reply with the result, "
    "severity, issues found, and what wasn't visible.\n\n"

    "<b>Duplicate guard</b>\n"
    "If the same media set has already been checked for this driver, I'll block it and tell you when "
    "the prior check ran. Record a fresh inspection if you need a new check.\n\n"

    "<b>Admin tools</b>\n"
    "• <code>/removedriver</code> — reply to a registered driver's message (group admin only)\n\n"

    "<b>Notes</b>\n"
    "• Both drivers and any group member can run <code>/check</code>; only registered drivers' media is accepted.\n"
    "• Times in messages are Eastern (ET); the bot auto-handles EDT/EST.\n"
)

GROUP_HELP_TEXT = _INTRO + _SETUP + _BODY
DM_HELP_TEXT = _INTRO + _BODY


@dp.message_handler(CommandHelp(), chat_type=[types.ChatType.GROUP, types.ChatType.SUPERGROUP])
async def bot_help_group(message: types.Message):
    await message.answer(GROUP_HELP_TEXT, parse_mode="HTML", disable_web_page_preview=True)


@dp.message_handler(CommandHelp(), chat_type=types.ChatType.PRIVATE)
async def bot_help_dm(message: types.Message):
    await message.answer(DM_HELP_TEXT, parse_mode="HTML", disable_web_page_preview=True)
