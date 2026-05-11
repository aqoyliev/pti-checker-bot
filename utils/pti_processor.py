import asyncio
import json
import logging
import os
import shutil
import tempfile

from aiogram import types

from data import config
from loader import bot
from test_pti import extract_frames, call_gemini, call_gemini_photos, delete_frames, parse_result


def format_result(data: dict) -> str:
    status = data.get("status", "?")
    severity = data.get("severity", "?")
    confidence = data.get("confidence", "?")
    image_quality = data.get("image_quality", "?")
    issues = data.get("issues", [])
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
        lines.append("\n<b>Not visible:</b>")
        lines.extend(f"  • {item}" for item in not_visible)

    resolved = data.get("previously_flagged_resolved", [])
    if resolved:
        lines.append("\n<b>Previously flagged — now resolved:</b>")
        lines.extend(f"  ✔ {item}" for item in resolved)

    if advice:
        lines.append(f"\n<b>Advice:</b> {advice}")

    return "\n".join(lines)


async def process_video(file, reply_to: types.Message, history: list[dict] | None = None) -> tuple[str | None, dict | None]:
    """Returns (formatted_text, raw_data) or (None, None) on failure."""
    status_msg = await reply_to.answer("Analyzing video...")

    tmp_path = None
    frames = []
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp_path = tmp.name

        file_info = await file.get_file()
        if config.LOCAL_SERVER_URL:
            await asyncio.to_thread(shutil.copy2, file_info.file_path, tmp_path)
        else:
            await bot.download_file(file_info.file_path, destination=tmp_path)

        frames = extract_frames(tmp_path)
        if not frames:
            await status_msg.edit_text("Could not extract frames from the video.")
            return None, None

        await status_msg.edit_text("Analyzing...")

        try:
            response = call_gemini(frames, history=history)
        finally:
            delete_frames(frames)
            frames = []

        try:
            data = parse_result(response)
            text = format_result(data)
        except (json.JSONDecodeError, KeyError):
            data = {}
            text = f"<b>PTI Result:</b>\n{response.text}"

        await status_msg.edit_text(text, parse_mode="HTML")
        return text, data

    except Exception as e:
        logging.exception("PTI video processing error")
        await status_msg.edit_text(f"An error occurred: {e}")
        return None, None
    finally:
        if frames:
            delete_frames(frames)
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


async def process_photos(photos: list[types.PhotoSize], reply_to: types.Message, history: list[dict] | None = None) -> tuple[str | None, dict | None]:
    count = len(photos)
    label = f"{count} photos" if count > 1 else "photo"
    status_msg = await reply_to.answer(f"Analyzing {label}...")

    tmp_paths: list[str] = []
    try:
        for photo in photos:
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp_paths.append(tmp.name)
            file_info = await photo.get_file()
            if config.LOCAL_SERVER_URL:
                await asyncio.to_thread(shutil.copy2, file_info.file_path, tmp_paths[-1])
            else:
                await bot.download_file(file_info.file_path, destination=tmp_paths[-1])

        response = await asyncio.to_thread(
            call_gemini_photos, [(p, "image/jpeg") for p in tmp_paths], history=history
        )

        try:
            data = parse_result(response)
            text = format_result(data)
        except (json.JSONDecodeError, KeyError):
            data = {}
            text = f"<b>PTI Result:</b>\n{response.text}"

        await status_msg.edit_text(text, parse_mode="HTML")
        return text, data

    except Exception as e:
        logging.exception("PTI photo processing error")
        await status_msg.edit_text(f"An error occurred: {e}")
        return None, None
    finally:
        for path in tmp_paths:
            try:
                os.remove(path)
            except OSError:
                pass


async def process_image_docs(docs: list[types.Document], reply_to: types.Message, history: list[dict] | None = None) -> tuple[str | None, dict | None]:
    count = len(docs)
    label = f"{count} photos" if count > 1 else "photo"
    status_msg = await reply_to.answer(f"Analyzing {label}...")

    tmp_paths: list[str] = []
    images: list[tuple[str, str]] = []
    try:
        for doc in docs:
            with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as tmp:
                tmp_paths.append(tmp.name)
            file_info = await doc.get_file()
            if config.LOCAL_SERVER_URL:
                await asyncio.to_thread(shutil.copy2, file_info.file_path, tmp_paths[-1])
            else:
                await bot.download_file(file_info.file_path, destination=tmp_paths[-1])
            images.append((tmp_paths[-1], doc.mime_type))

        response = await asyncio.to_thread(call_gemini_photos, images, history=history)

        try:
            data = parse_result(response)
            text = format_result(data)
        except (json.JSONDecodeError, KeyError):
            data = {}
            text = f"<b>PTI Result:</b>\n{response.text}"

        await status_msg.edit_text(text, parse_mode="HTML")
        return text, data

    except Exception as e:
        logging.exception("PTI image doc processing error")
        await status_msg.edit_text(f"An error occurred: {e}")
        return None, None
    finally:
        for path in tmp_paths:
            try:
                os.remove(path)
            except OSError:
                pass
