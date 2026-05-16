from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile

import httpcore
import httpx
from aiogram import types
from google.genai import errors as genai_errors

_TRANSIENT_ERRORS = (genai_errors.ServerError, httpcore.RemoteProtocolError, httpx.RemoteProtocolError, httpx.ConnectError, httpx.TimeoutException)

from data import config
from loader import bot
from test_pti import extract_frames, call_gemini, call_gemini_photos, delete_frames, parse_result, VideoTooLongError, MAX_FRAMES

_GEMINI_RETRY_DELAYS = (5, 10, 20)  # seconds; 3 retries after the initial attempt


def _fmt_timestamp(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


async def _call_gemini_with_retry(fn, *args, **kwargs):
    last_exc = None
    for attempt, delay in enumerate((0,) + _GEMINI_RETRY_DELAYS):
        if delay:
            await asyncio.sleep(delay)
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except _TRANSIENT_ERRORS as e:
            last_exc = e
            delay_next = _GEMINI_RETRY_DELAYS[attempt] if attempt < len(_GEMINI_RETRY_DELAYS) else 0
            logging.warning(f"Gemini transient error on attempt {attempt + 1} ({type(e).__name__}); retrying in {delay_next}s")
            continue
    raise last_exc


def _media_summary(photos: int, videos: int) -> str | None:
    if not photos and not videos:
        return None
    parts: list[str] = []
    if photos:
        parts.append(f"{photos} photo{'s' if photos != 1 else ''}")
    if videos:
        parts.append(f"{videos} video{'s' if videos != 1 else ''}")
    return f"📎 Checked: {' and '.join(parts)}"


_SEVERITY_ICON = {"NONE": "🟢", "MINOR": "🟡", "MAJOR": "🟠", "CRITICAL": "🔴"}


def format_result(data: dict, photos: int = 0, videos: int = 0, driver_name: str | None = None) -> str:
    from html import escape

    status = data.get("status", "?")
    severity = data.get("severity", "") or ""
    confidence = data.get("confidence", "") or ""
    image_quality = data.get("image_quality", "") or ""
    issues = data.get("issues", []) or []
    clean = data.get("checked_clean", []) or []
    not_visible = data.get("what_was_not_visible", []) or []
    advice = (data.get("advice") or "").strip()

    status_icon = "✅" if status == "PASS" else "❌"
    lines = [f"{status_icon} <b>PTI Result: {escape(status)}</b>"]
    if driver_name:
        lines.append(f"👤 <b>Driver:</b> {escape(driver_name)}")
    if severity and severity != "NONE":
        sev_icon = _SEVERITY_ICON.get(severity, "")
        lines.append(f"{sev_icon} <b>Severity:</b> {escape(severity)}".lstrip())
    meta_bits = []
    if confidence:
        meta_bits.append(f"Confidence: {escape(confidence)}")
    if image_quality:
        meta_bits.append(f"Image quality: {escape(image_quality)}")
    if meta_bits:
        lines.append(" | ".join(meta_bits))
    summary = _media_summary(photos, videos)
    if summary:
        lines.append(summary)

    if issues:
        lines.append("")
        lines.extend(f"❌ {escape(str(i))}" for i in issues)

    if clean:
        if not issues:
            lines.append("")
        lines.extend(f"✅ {escape(str(c))}" for c in clean)

    if not_visible:
        lines.append("")
        lines.append(f"👁 <b>Not visible:</b> {escape(', '.join(str(n) for n in not_visible))}")

    if advice:
        lines.append("")
        lines.append(f"💡 {escape(advice)}")

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

        try:
            frames = extract_frames(tmp_path)
        except VideoTooLongError as e:
            mins = int(e.duration) // 60
            await status_msg.edit_text(
                f"⚠️ Video is {mins} min long — too long for a PTI inspection. Please send a video under 15 minutes."
            )
            return None, None
        if not frames:
            await status_msg.edit_text("Could not extract frames from the video.")
            return None, None

        await status_msg.edit_text(f"Analyzing {len(frames)} frame(s)...")

        try:
            response = await _call_gemini_with_retry(call_gemini, frames, history=history)
        finally:
            delete_frames(frames)
            frames = []

        try:
            data = parse_result(response)
            text = format_result(data, photos=0, videos=1)
        except (json.JSONDecodeError, KeyError):
            data = {}
            text = f"<b>PTI Result:</b>\n{response.text}"

        await status_msg.edit_text(text, parse_mode="HTML")
        return text, data

    except genai_errors.ServerError:
        logging.warning("Gemini still unavailable after retries (video flow)")
        await status_msg.edit_text("The analysis service is overloaded right now. Please try /check again in a minute.")
        return None, None
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

        response = await _call_gemini_with_retry(
            call_gemini_photos, [(p, "image/jpeg") for p in tmp_paths], history=history
        )

        try:
            data = parse_result(response)
            text = format_result(data, photos=len(photos), videos=0)
        except (json.JSONDecodeError, KeyError):
            data = {}
            text = f"<b>PTI Result:</b>\n{response.text}"

        await status_msg.edit_text(text, parse_mode="HTML")
        return text, data

    except genai_errors.ServerError:
        logging.warning("Gemini still unavailable after retries (photo flow)")
        await status_msg.edit_text("The analysis service is overloaded right now. Please try /check again in a minute.")
        return None, None
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


async def process_mixed_media(
    items,
    reply_to: types.Message,
    history: list[dict] | None = None,
    driver_name: str | None = None,
) -> tuple[str | None, dict | None, types.Message | None]:
    """Process a mix of photos, image docs, and videos as a single PTI inspection.

    `items` is a list of dicts: {"kind": "photo"|"image_doc"|"video"|"video_note"|"video_doc", "obj": <telegram file obj>}

    Returns ``(text, data, status_msg)`` — the formatted PASS/FAIL text, the raw
    parsed dict, and the bot's status message in the chat. On success the status
    message is left with the "Analyzing..." progress text; the caller chooses what
    to render there (full result, or a hold message during vehicle-change vote).
    On error the status message is edited with the error and ``(None, None, status_msg)``
    is returned.
    """
    try:
        status_msg = await reply_to.reply(
            f"Analyzing {len(items)} item(s)...",
            allow_sending_without_reply=False,
        )
    except Exception as e:
        logging.warning(
            f"PTI reply-quote unavailable ({type(e).__name__}: {e}); sending non-reply status. "
            f"Often means the inspected message was posted by an anonymous admin."
        )
        status_msg = await reply_to.answer(f"Analyzing {len(items)} item(s)...")

    photo_count = sum(1 for it in items if it["kind"] in ("photo", "image_doc"))
    video_count = sum(1 for it in items if it["kind"] in ("video", "video_note", "video_doc"))

    tmp_paths: list[str] = []
    video_frames: list[tuple[float, str]] = []
    images: list[tuple[str, str]] = []
    skipped = 0
    try:
        for item in items:
            kind = item["kind"]
            obj = item["obj"]

            if kind == "photo":
                suffix, mime = ".jpg", "image/jpeg"
            elif kind == "image_doc":
                suffix, mime = ".img", obj.mime_type or "image/jpeg"
            elif kind in ("video", "video_note", "video_doc"):
                suffix, mime = ".mp4", None
            else:
                continue

            try:
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                    tmp_path = tmp.name
                tmp_paths.append(tmp_path)
                file_info = await obj.get_file()
                if config.LOCAL_SERVER_URL:
                    await asyncio.to_thread(shutil.copy2, file_info.file_path, tmp_path)
                else:
                    await bot.download_file(file_info.file_path, destination=tmp_path)
            except Exception:
                logging.warning(f"Skipping {kind} item — could not download", exc_info=True)
                skipped += 1
                continue

            if mime is None:
                try:
                    extracted = extract_frames(tmp_path)
                except VideoTooLongError as e:
                    mins = int(e.duration) // 60
                    await status_msg.edit_text(
                        f"⚠️ Video is {mins} min long — too long for a PTI inspection. Please send a video under 15 minutes."
                    )
                    return None, None, status_msg
                video_frames.extend(extracted)
            else:
                images.append((tmp_path, mime))

        capped_frames = video_frames
        if len(video_frames) > MAX_FRAMES:
            step = len(video_frames) / MAX_FRAMES
            capped_frames = [video_frames[int(i * step)] for i in range(MAX_FRAMES)]

        photo_labels = [(p, m, f"Photo {i + 1}") for i, (p, m) in enumerate(images)]
        video_labels = [(p, "image/jpeg", f"Video frame at {_fmt_timestamp(t)}") for t, p in capped_frames]
        all_images = photo_labels + video_labels
        if not all_images:
            msg = "Could not download any of the media (files may be too large)." if skipped else "No usable media to analyze."
            await status_msg.edit_text(msg)
            return None, None, status_msg

        cap_note = f" (capped from {len(video_frames)})" if len(capped_frames) < len(video_frames) else ""
        logging.info(
            f"PTI mixed-media: {len(images)} photo(s) + {len(capped_frames)} video frame(s){cap_note} "
            f"= {len(all_images)} image(s) → Gemini"
        )
        parts: list[str] = []
        if photo_count:
            parts.append(f"{photo_count} photo{'s' if photo_count != 1 else ''}")
        if video_count:
            parts.append(f"{video_count} video{'s' if video_count != 1 else ''}")
        await status_msg.edit_text(f"Analyzing {' and '.join(parts) or 'media'}...")
        response = await _call_gemini_with_retry(call_gemini_photos, all_images, history=history)

        try:
            data = parse_result(response)
            text = format_result(data, photos=photo_count, videos=video_count, driver_name=driver_name)
        except (json.JSONDecodeError, KeyError):
            data = {}
            text = f"<b>PTI Result:</b>\n{response.text}"

        return text, data, status_msg

    except genai_errors.ServerError:
        logging.warning("Gemini still unavailable after retries (mixed-media flow)")
        await status_msg.edit_text("The analysis service is overloaded right now. Please try /check again in a minute.")
        return None, None, status_msg
    except ValueError as e:
        logging.warning(f"Gemini returned unusable response: {e}")
        await status_msg.edit_text(
            "The analysis service could not read this PTI (response was empty or blocked). "
            "Please re-record and try /check again."
        )
        return None, None, status_msg
    except Exception as e:
        logging.exception("PTI mixed-media processing error")
        await status_msg.edit_text(f"An error occurred: {e}")
        return None, None, status_msg
    finally:
        if video_frames:
            delete_frames(video_frames)
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
            text = format_result(data, photos=len(docs), videos=0)
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
