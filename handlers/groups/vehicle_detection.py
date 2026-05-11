import asyncio
import json
import logging
import os
import shutil
import tempfile

from aiogram import types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from loader import bot, dp
from data import config
from utils.db import get_group, get_drivers, set_group_unit
from test_pti import call_gemini_photos

VEHICLE_ID_PROMPT = """Look at these images and extract vehicle identification information.
Return ONLY a raw JSON object — no markdown, no code fences:
{
  "type": "truck" or "trailer" or null,
  "unit_number": "the unit/fleet number painted on the vehicle or null if not visible",
  "plate": "license plate number or null if not visible"
}"""

_pending_confirmations: dict[str, dict] = {}


async def _extract_vehicle_info(media_items) -> dict | None:
    from handlers.groups.monitoring import BufferedMessage

    tmp_paths: list[str] = []
    images: list[tuple[str, str]] = []
    try:
        for item in media_items:
            if item.content_type == "photo" and item.photo_size:
                suffix, mime = ".jpg", "image/jpeg"
                file_obj = item.photo_size
            elif item.content_type == "document" and item.document and (item.mime_type or "").startswith("image/"):
                suffix, mime = ".img", item.mime_type
                file_obj = item.document
            elif item.content_type in ("video", "video_note"):
                continue
            else:
                continue

            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp_paths.append(tmp.name)
            file_info = await file_obj.get_file()
            if config.LOCAL_SERVER_URL:
                await asyncio.to_thread(shutil.copy2, file_info.file_path, tmp_paths[-1])
            else:
                await bot.download_file(file_info.file_path, destination=tmp_paths[-1])
            images.append((tmp_paths[-1], mime))

        if not images:
            return None

        from google import genai
        from google.genai import types as genai_types

        api_key = os.getenv("GEMINI_API_KEY")
        client = genai.Client(api_key=api_key)

        n = len(images)
        parts = []
        for i, (path, mime_type) in enumerate(images):
            with open(path, "rb") as f:
                parts.append(genai_types.Part.from_bytes(data=f.read(), mime_type=mime_type))
        parts.append(VEHICLE_ID_PROMPT)

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            config=genai_types.GenerateContentConfig(temperature=0.1),
            contents=parts,
        )

        return json.loads(response.text)

    except Exception:
        logging.exception("Vehicle info extraction error")
        return None
    finally:
        for path in tmp_paths:
            try:
                os.remove(path)
            except OSError:
                pass


async def process_vehicle_media(group_id: int, media_items, chat: types.Chat):
    vehicle_info = await _extract_vehicle_info(media_items)
    if not vehicle_info:
        return

    unit_number = vehicle_info.get("unit_number")
    plate = vehicle_info.get("plate")
    vehicle_type = vehicle_info.get("type")

    if not unit_number and not plate:
        return

    group = await get_group(group_id)
    current_unit = group.get("unit_number") if group else None

    if unit_number and unit_number == current_unit:
        return

    drivers = await get_drivers(group_id)
    driver_names = " & ".join(d["name"] for d in drivers) if drivers else "Driver"

    info_lines = []
    if vehicle_type:
        info_lines.append(f"Type: <b>{vehicle_type.capitalize()}</b>")
    if unit_number:
        info_lines.append(f"Unit: <b>{unit_number}</b>")
    if plate:
        info_lines.append(f"Plate: <b>{plate}</b>")
    if current_unit:
        info_lines.append(f"Previous unit: <b>{current_unit}</b>")

    info_text = "\n".join(info_lines)

    confirmation_id = f"{group_id}_{unit_number or plate}"
    _pending_confirmations[confirmation_id] = {
        "group_id": group_id,
        "unit_number": unit_number,
        "plate": plate,
    }

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Confirm", callback_data=f"vc_confirm:{confirmation_id}"),
        InlineKeyboardButton(text="❌ Reject", callback_data=f"vc_reject:{confirmation_id}"),
    ]])

    await bot.send_message(
        group_id,
        f"🚛 <b>Vehicle change detected</b> for {driver_names}\n\n{info_text}\n\n"
        f"Please confirm this change:",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("vc_confirm:"))
async def on_vehicle_confirm(callback: types.CallbackQuery):
    confirmation_id = callback.data.split(":", 1)[1]
    pending = _pending_confirmations.pop(confirmation_id, None)
    if not pending:
        await callback.answer("This confirmation has already been handled.")
        return

    group_id = pending["group_id"]
    unit_number = pending["unit_number"]

    member = await bot.get_chat_member(group_id, callback.from_user.id)
    drivers = await get_drivers(group_id)
    driver_ids = {d["user_id"] for d in drivers}

    is_admin = member.status in ("administrator", "creator")
    is_driver = callback.from_user.id in driver_ids

    if not is_admin and not is_driver:
        await callback.answer("Only admins or registered drivers can confirm this.")
        return

    if unit_number:
        await set_group_unit(group_id, unit_number)

    await callback.message.edit_text(
        callback.message.text + f"\n\n✅ Confirmed by {callback.from_user.full_name}.",
        parse_mode="HTML",
        reply_markup=None,
    )
    await callback.answer()


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("vc_reject:"))
async def on_vehicle_reject(callback: types.CallbackQuery):
    confirmation_id = callback.data.split(":", 1)[1]
    pending = _pending_confirmations.pop(confirmation_id, None)
    if not pending:
        await callback.answer("This confirmation has already been handled.")
        return

    group_id = pending["group_id"]
    member = await bot.get_chat_member(group_id, callback.from_user.id)
    drivers = await get_drivers(group_id)
    driver_ids = {d["user_id"] for d in drivers}

    is_admin = member.status in ("administrator", "creator")
    is_driver = callback.from_user.id in driver_ids

    if not is_admin and not is_driver:
        await callback.answer("Only admins or registered drivers can reject this.")
        return

    await callback.message.edit_text(
        callback.message.text + f"\n\n❌ Rejected by {callback.from_user.full_name}.",
        parse_mode="HTML",
        reply_markup=None,
    )
    await callback.answer()
