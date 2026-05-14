from aiogram import types
from aiogram.dispatcher.filters.builtin import CommandHelp

from loader import dp

HELP_TEXT = (
    "🛠️ <b>PTI Checker Bot — Help</b>\n\n"
    "I review pre-trip inspection (PTI) media and decide PASS/FAIL using a DOT-trained model.\n\n"

    "<b>Setup (one-time per group)</b>\n"
    "When I'm added to a group, I read the group name and bio to detect the unit number and "
    "driver names. If anything is missing, anyone in the group can run:\n"
    "• <code>/setunit &lt;unit_number&gt;</code> — set the truck unit (needs 3 confirms)\n"
    "• <code>/adddriver Driver Name</code> — reply to the driver's message (needs 3 confirms)\n"
    "Each command opens a vote — tap ✅ Confirm or ❌ Reject. 3 confirms saves it; 3 rejects asks you to re-enter. "
    "If a vote stalls, I'll re-post the poll up to 3 times every 10 minutes.\n\n"

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
    "• I need admin rights with <b>Restrict members</b> to mute/unmute drivers based on PTI compliance.\n"
    "• Times in messages are Eastern (ET); the bot auto-handles EDT/EST.\n"
)


@dp.message_handler(CommandHelp())
async def bot_help(message: types.Message):
    await message.answer(HELP_TEXT, parse_mode="HTML", disable_web_page_preview=True)
