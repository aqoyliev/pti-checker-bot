import logging
import os
import tempfile
import json

from aiogram import types
from aiogram.types import ContentType

from loader import dp
from test_pti import extract_frames, call_gemini, delete_frames, parse_result


def _format_result(data: dict) -> str:
    status = data.get("status", "?")
    severity = data.get("severity", "?")
    confidence = data.get("confidence", "?")
    image_quality = data.get("image_quality", "?")
    issues = data.get("issues", [])
    visible = data.get("what_was_visible", [])
    not_visible = data.get("what_was_not_visible", [])
    advice = data.get("advice", "")

    status_icon = "✅" if status == "PASS" else "❌"
    severity_icon = {"NONE": "🟢", "MINOR": "🟡", "MAJOR": "🟠", "CRITICAL": "🔴"}.get(severity, "⚪")

    lines = [
        f"{status_icon} <b>PTI Result: {status}</b>",
        f"{severity_icon} Severity: <b>{severity}</b>",
        f"Confidence: <b>{confidence}</b>  |  Image quality: <b>{image_quality}</b>",
    ]

    if issues:
        lines.append("\n<b>Issues found:</b>")
        lines.extend(f"  • {issue}" for issue in issues)

    if not_visible:
        lines.append("\n<b>Not visible in video:</b>")
        lines.extend(f"  • {item}" for item in not_visible)

    if advice:
        lines.append(f"\n<b>Advice:</b> {advice}")

    return "\n".join(lines)


@dp.message_handler(content_types=[ContentType.VIDEO, ContentType.VIDEO_NOTE, ContentType.DOCUMENT])
async def handle_pti_video(message: types.Message):
    file = message.video or message.video_note or message.document

    if message.document and not (message.document.mime_type or "").startswith("video/"):
        await message.answer("Iltimos, PTI tekshiruvi uchun video yuboring.")
        return

    status_msg = await message.answer("Video qabul qilindi. Kadrlar ajratilmoqda...")

    tmp_path = None
    frames = []
    try:
        suffix = ".mp4"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name

        await file.download(destination=tmp_path)

        frames = extract_frames(tmp_path)
        if not frames:
            await status_msg.edit_text("Xato: Videodan kadrlar ajratib bo'lmadi.")
            return

        await status_msg.edit_text(f"{len(frames)} kadr Gemini AI ga yuborilmoqda...")

        try:
            response = call_gemini(frames)
        finally:
            delete_frames(frames)
            frames = []

        try:
            data = parse_result(response)
            text = _format_result(data)
        except (json.JSONDecodeError, KeyError):
            text = f"<b>PTI natijasi:</b>\n{response.text}"

        await status_msg.edit_text(text, parse_mode="HTML")

    except Exception as e:
        logging.exception("PTI video processing error")
        await status_msg.edit_text(f"Xato yuz berdi: {e}")
    finally:
        if frames:
            delete_frames(frames)
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
