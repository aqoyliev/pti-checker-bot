from aiogram import types
from aiogram.dispatcher.filters.builtin import CommandHelp

from loader import dp

# The help text describes what the bot actually does today. Two things it
# deliberately does NOT say: how to configure a group (drivers are never asked
# to -- the fleet's admins do that from their side), and which AI vendor is
# behind the verdict.

_INTRO = (
    "🛠️ <b>PTI Checker Bot — Help</b>\n\n"
    "I review pre-trip inspection (PTI) videos and photos and reply with "
    "PASS or FAIL, the defects found, and anything that wasn't filmed.\n\n"
)

_GROUP = (
    "<b>Sending a PTI</b>\n"
    "1. Film the walkaround and post the video (or photos) in this group.\n"
    "2. Reply to it with <code>/check</code> — or post the video as a reply to "
    "one of my messages, and I'll start on my own.\n"
    "3. The result comes back as a reply to your video, usually within a few minutes.\n\n"

    "<b>What PASS / FAIL means</b>\n"
    "PASS means every required area was filmed: brake pads, lights (shown "
    "working), tires, mirrors, windshield, air lines, frame and the trailer ABS "
    "lamp. FAIL means something was not filmed — re-film the areas I list under "
    "<i>Not visible</i>. Defects are reported either way; they never change the verdict.\n\n"

    "<b>Good to know</b>\n"
    "• Only a registered driver's video counts. Anyone in the group may type /check.\n"
    "• A video you already sent (same length and size) is rejected — record a new one.\n"
    "• Videos over 15 minutes are too long to analyse.\n"
    "• Group setup — the unit number and who the drivers are — is done by the "
    "fleet's admins, not in the chat.\n"
)

_DM = (
    "I only inspect videos posted in your <b>driver group</b>. Send the PTI "
    "there and reply to it with <code>/check</code>.\n"
)

GROUP_HELP_TEXT = _INTRO + _GROUP
DM_HELP_TEXT = _INTRO + _DM


@dp.message_handler(CommandHelp(), chat_type=[types.ChatType.GROUP, types.ChatType.SUPERGROUP])
async def bot_help_group(message: types.Message):
    await message.answer(GROUP_HELP_TEXT, parse_mode="HTML", disable_web_page_preview=True)


@dp.message_handler(CommandHelp(), chat_type=types.ChatType.PRIVATE)
async def bot_help_dm(message: types.Message):
    await message.answer(DM_HELP_TEXT, parse_mode="HTML", disable_web_page_preview=True)
