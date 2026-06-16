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

_TRANSIENT_NET_ERRORS = (httpcore.RemoteProtocolError, httpx.RemoteProtocolError, httpx.ConnectError, httpx.TimeoutException)

OVERLOAD_USER_MESSAGE = "The analysis service is overloaded right now. Please try /check again in a minute."


def _is_service_overload(e: Exception) -> bool:
    """5xx ServerError or 429 quota — both surfaced to users as 'overloaded'."""
    if isinstance(e, genai_errors.ServerError):
        return True
    return isinstance(e, genai_errors.ClientError) and getattr(e, "code", None) == 429


from data import config
from loader import bot
from test_pti import extract_frames, call_gemini_photos, delete_frames, parse_result, VideoTooLongError, MAX_FRAMES

_GEMINI_RETRY_DELAYS = (15, 30, 60)  # seconds; 3 retries after the initial attempt

# Global cap on concurrent PTI analyses (frame extraction + Gemini). Bounds CPU,
# memory, disk, thread-pool, and Gemini-quota pressure when submissions arrive in
# a burst; extra inspections queue here instead of overwhelming the host.
# Created lazily so it binds to the running event loop on first use.
_analysis_semaphore: asyncio.Semaphore | None = None


def _get_analysis_slot() -> asyncio.Semaphore:
    global _analysis_semaphore
    if _analysis_semaphore is None:
        _analysis_semaphore = asyncio.Semaphore(max(1, config.PTI_MAX_CONCURRENCY))
    return _analysis_semaphore


def _fmt_timestamp(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


async def _call_gemini_with_retry(fn, *args, **kwargs):
    last_exc = None
    all_delays = (0,) + _GEMINI_RETRY_DELAYS
    for attempt, delay in enumerate(all_delays):
        if delay:
            await asyncio.sleep(delay)
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except Exception as e:
            if not (isinstance(e, _TRANSIENT_NET_ERRORS) or _is_service_overload(e)):
                raise
            last_exc = e
            if attempt + 1 < len(all_delays):
                delay_next = all_delays[attempt + 1]
                logging.warning(f"Gemini transient error on attempt {attempt + 1} ({type(e).__name__}); retrying in {delay_next}s")
            else:
                logging.warning(f"Gemini transient error on attempt {attempt + 1} ({type(e).__name__}); no more retries")
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

# Phrases that indicate a vague conclusion rather than concrete visual evidence.
# Issues whose evidence (or text) contains any of these are dropped as likely hallucinations.
_BANNED_EVIDENCE_PHRASES = (
    "severe wear",
    "heavy wear",
    "tires show wear",
    "worn tires",
    "worn tire",
    "worn dual",
    "worn inner dual",
    "worn outer dual",
    "worn drive tire",
    "worn steer tire",
    "worn trailer tire",
    "tire wear",
    "inspect tire",
    "inspect trailer tire",
    "inspect drive tire",
    "replace soon",
    "replace as needed",
    "tread is low",
    "tread depth low",
    "outer shoulder is smooth",
    "outer shoulder bald",
    "shoulder is smooth",
    "shoulder bald",
    "shoulder is bald",
    "below 4/32",
    "below 2/32",
    "wear bars",
    "tire is worn",
    "visible damage",
    "severe visible wear",
    "different tread pattern",
    "different tire pattern",
    "mismatched tread",
    "mismatched tire",
)
_MIN_EVIDENCE_CHARS = 20


def _issue_text(item) -> str:
    if isinstance(item, dict):
        return (item.get("text") or "").strip()
    return str(item).strip()


def _issue_evidence(item) -> str:
    if isinstance(item, dict):
        return (item.get("evidence") or "").strip()
    return ""


def _issue_is_oos(item) -> bool:
    """True if the model flagged this defect as an Out-of-Service condition."""
    return isinstance(item, dict) and bool(item.get("oos"))


def has_oos_defect(data: dict) -> bool:
    """True if any reported issue is an out-of-service defect.

    OOS is a REPORTING label only — it does NOT decide PASS/FAIL (that is purely
    completeness, see apply_completeness_verdict). format_result still lists OOS
    issues under their own "Out-of-service defects" heading so the driver sees
    them flagged. Call AFTER filter_hallucinated_issues so dropped hallucinations
    don't count.
    """
    return any(_issue_is_oos(i) for i in (data.get("issues") or []))


# The 9 in-scope PTI inspection areas, by their canonical "checked_clean" labels.
# A complete inspection must show every one of these (the trailer ABS lamp included
# — it must be filmed even though it is normally OFF). Drivers are NOT required to
# film the trailer plate or unit number, so those are not areas here. The fire
# extinguisher & triangle are checked separately (see "fire_extinguisher_shown" in
# format_result) as an advisory reminder — they no longer affect completeness/PASS-FAIL.
REQUIRED_AREAS = (
    "Brake pads",
    "Lights",
    "Tires",
    "Mirrors",
    "Under hood",
    "Windshield",
    "Air lines",
    "Frame",
    "ABS lamp",
)
_REQUIRED_AREAS_BY_KEY = {a.lower(): a for a in REQUIRED_AREAS}


def _missing_required_areas(data: dict) -> list[str]:
    """Required inspection areas the driver did not adequately film.

    Reads the model's structured ``missing_areas`` field, restricted to the known
    9-area vocabulary (so free-text noise can't trip the verdict) and de-duped
    against anything the model already marked clean. As a safety net, a
    ``what_was_not_visible`` entry that exactly matches a canonical area label also
    counts — so the rule still fires if the model under-populates ``missing_areas``.

    The ABS lamp is enforced harder: it must be positively accounted for — filmed
    and off (in ``checked_clean``), filmed and on (reported as an ABS issue), or
    else it counts as missing. This way a driver can't pass by simply omitting the
    ABS lamp from the footage (it isn't enough for the model to just not mention it).
    """
    clean = {str(c).strip().lower() for c in (data.get("checked_clean") or [])}
    seen: set[str] = set()
    missing: list[str] = []
    candidates = list(data.get("missing_areas") or []) + list(data.get("what_was_not_visible") or [])
    for item in candidates:
        key = str(item).strip().lower()
        canon = _REQUIRED_AREAS_BY_KEY.get(key)
        if canon and key not in clean and canon not in seen:
            seen.add(canon)
            missing.append(canon)

    # ABS-lamp safety net: require it to be accounted for, not merely unmentioned.
    if "ABS lamp" not in seen and "abs lamp" not in clean:
        abs_in_issue = any("abs" in _issue_text(i).lower() for i in (data.get("issues") or []))
        if not abs_in_issue:
            missing.append("ABS lamp")

    return missing


def apply_completeness_verdict(data: dict) -> bool:
    """Set PASS/FAIL — based ONLY on completeness, never on OOS or any defect.

    The inspection FAILs iff a required area was never filmed (incomplete), so a
    driver can't pass by simply not filming a component. Defects — including
    out-of-service ones like an illuminated ABS lamp — are still reported and
    labeled, but they do NOT change the verdict. Severity rates the worst defect
    for the driver's awareness and is independent of PASS/FAIL. Stores the
    normalized list back on ``data["missing_areas"]``. Returns True if incomplete
    (FAIL). Call AFTER filter_hallucinated_issues.
    """
    missing = _missing_required_areas(data)
    data["missing_areas"] = missing
    issues = data.get("issues") or []
    if missing:
        data["status"] = "FAIL"
        data["severity"] = "CRITICAL" if has_oos_defect(data) else "MAJOR"
        # The "Incomplete" section already lists the un-filmed areas; no extra advice line.
        data["advice"] = ""
        return True
    data["status"] = "PASS"
    if has_oos_defect(data):
        data["severity"] = "CRITICAL"
    elif issues:
        sev = (data.get("severity") or "").upper()
        data["severity"] = sev if sev in ("MINOR", "MAJOR") else "MINOR"
    else:
        data["severity"] = "NONE"
    data.setdefault("advice", "")
    return False


def filter_hallucinated_issues(data: dict) -> int:
    """Drop issues whose evidence is missing/too short or matches a banned conclusion phrase.

    If every issue is dropped, flip status -> PASS and severity -> NONE so the driver
    sees a clean result instead of a FAIL with no defects listed.

    Returns the number of issues dropped (for logging).
    """
    raw = data.get("issues") or []
    kept = []
    dropped = 0
    for item in raw:
        text = _issue_text(item)
        evidence = _issue_evidence(item)
        if not text:
            dropped += 1
            logging.info(f"Filtered issue: empty text — {item!r}")
            continue
        # Legacy plain-string issues (no evidence field): keep but log so we notice the
        # model didn't follow the new schema.
        if isinstance(item, str):
            logging.info(f"Plain-string issue kept (legacy format): {text!r}")
            kept.append(item)
            continue
        if len(evidence) < _MIN_EVIDENCE_CHARS:
            dropped += 1
            logging.info(f"Filtered issue '{text}': evidence too short ({len(evidence)} chars) — {evidence!r}")
            continue
        haystack = (text + " " + evidence).lower()
        matched = next((p for p in _BANNED_EVIDENCE_PHRASES if p in haystack), None)
        if matched:
            dropped += 1
            logging.info(f"Filtered issue '{text}': banned phrase '{matched}' in evidence — {evidence!r}")
            continue
        kept.append(item)
    data["issues"] = kept
    if dropped and not kept:
        logging.info(f"All {dropped} issue(s) filtered as hallucinations — flipping status to PASS")
        data["status"] = "PASS"
        data["severity"] = "NONE"
        # Drop the now-misleading advice and let format_result render a clean PASS
        data["advice"] = ""
    return dropped


def format_result(data: dict, photos: int = 0, videos: int = 0, driver_name: str | None = None) -> str:
    from html import escape

    status = data.get("status", "?")
    severity = data.get("severity", "") or ""
    confidence = data.get("confidence", "") or ""
    image_quality = data.get("image_quality", "") or ""
    issues = data.get("issues", []) or []
    clean = data.get("checked_clean", []) or []
    missing_areas = data.get("missing_areas", []) or []
    not_visible = data.get("what_was_not_visible", []) or []
    advice = (data.get("advice") or "").strip()
    fire_extinguisher_shown = data.get("fire_extinguisher_shown")

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

    oos_issues = [t for t in (_issue_text(i) for i in issues if _issue_is_oos(i)) if t]
    advisory_issues = [t for t in (_issue_text(i) for i in issues if not _issue_is_oos(i)) if t]

    if oos_issues:
        lines.append("")
        lines.append("🛑 <b>Out-of-service defects:</b>")
        lines.extend(f"❌ {escape(t)}" for t in oos_issues)

    if advisory_issues:
        lines.append("")
        lines.append("⚠️ <b>Advisories (not out-of-service):</b>")
        lines.extend(f"• {escape(t)}" for t in advisory_issues)

    missing_keys = {str(a).strip().lower() for a in missing_areas}
    if missing_areas:
        lines.append("")
        n = len(missing_areas)
        lines.append(f"🚧 <b>Incomplete — {n} required area{'s' if n != 1 else ''} weren't filmed</b> (see ❌ below):")

    # Always show the full checklist of required components, not just the ones
    # the model happened to mention — so the driver/admin can see at a glance
    # which areas were verified clean, flagged, or never filmed.
    clean_keys = {str(c).strip().lower() for c in clean}
    lines.append("")
    lines.append("📋 <b>Components inspected:</b>")
    for area in REQUIRED_AREAS:
        key = area.lower()
        icon = "❌" if key in missing_keys else "✅" if key in clean_keys else "⚠️"
        lines.append(f"{icon} {escape(area)}")

    # Fire extinguisher & triangle are advisory-only — not a required area, so they
    # never affect completeness/PASS-FAIL. Just remind the driver if never filmed.
    if fire_extinguisher_shown is False:
        lines.append("")
        lines.append("🧯 <b>Reminder:</b> film the fire extinguisher & warning triangle next time.")

    # Don't repeat missing areas in the free-text "Not visible" line.
    not_visible = [n for n in not_visible if str(n).strip().lower() not in missing_keys]
    if not_visible:
        lines.append("")
        lines.append(f"👁 <b>Not visible:</b> {escape(', '.join(str(n) for n in not_visible))}")

    if advice:
        lines.append("")
        lines.append(f"💡 {escape(advice)}")

    return "\n".join(lines)


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

    # Bound concurrent analyses so a burst of submissions queues instead of
    # exhausting CPU/memory/threads/Gemini quota. Tell the driver if they wait.
    slot = _get_analysis_slot()
    if slot.locked():
        try:
            await status_msg.edit_text(
                "📈 High volume right now — your PTI is queued and will start in a moment…"
            )
        except Exception:
            pass
    await slot.acquire()
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
                    extracted = await asyncio.to_thread(extract_frames, tmp_path)
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
            filter_hallucinated_issues(data)
            # PASS/FAIL depends ONLY on completeness; OOS defects are reported but
            # never fail the inspection.
            apply_completeness_verdict(data)
            text = format_result(data, photos=photo_count, videos=video_count, driver_name=driver_name)
        except (json.JSONDecodeError, KeyError):
            data = {}
            text = f"<b>PTI Result:</b>\n{response.text}"

        return text, data, status_msg

    except (genai_errors.ServerError, genai_errors.ClientError) as e:
        if not _is_service_overload(e):
            logging.exception("PTI mixed-media processing error")
            await status_msg.edit_text(f"An error occurred: {e}")
            return None, None, status_msg
        logging.warning(f"Gemini overloaded after retries (mixed-media flow): {type(e).__name__}")
        await status_msg.edit_text(OVERLOAD_USER_MESSAGE)
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
        slot.release()
        if video_frames:
            delete_frames(video_frames)
        for path in tmp_paths:
            try:
                os.remove(path)
            except OSError:
                pass
