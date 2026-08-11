from __future__ import annotations

import asyncio
import json
import logging
import os
import re
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
from utils.gemini import extract_frames, call_gemini_photos, call_gemini_tires, delete_frames, parse_result, get_api_keys, VideoTooLongError, MAX_FRAMES

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


def _should_split(num_images: int, num_keys: int) -> bool:
    """Whether to split this inspection's frames across keys instead of one whole call.

    Split whenever it's enabled, there's more than one key, and there are at least
    two images (one image can't be chunked — it stays a single call to the first
    key, with failover backing it up).
    """
    return config.PTI_SPLIT_FRAMES and num_keys > 1 and num_images >= 2


def _split_strided(items: list, n: int) -> list[list]:
    """Split `items` into at most `n` near-equal chunks, STRIDED not contiguous: chunk
    i gets items[i::n] (210 frames over 3 keys -> indices 0,3,6… / 1,4,7… / 2,5,8…).

    Strided so each chunk is a thin sample spanning the WHOLE walkaround, not one
    contiguous third of it. That keeps every chunk seeing a bit of every area (lights,
    tires, ABS lamp, …), so the merged completeness verdict stays close to a single
    whole-video pass — contiguous chunks each saw only a third and under-reported areas
    as un-filmed. Never returns more chunks than items, so empty chunks can't happen.
    """
    n = max(1, min(n, len(items)))
    return [items[i::n] for i in range(n)]


async def _call_gemini_with_retry(fn, *args, **kwargs):
    """Run a Gemini call with API-key failover.

    Tries each key from get_api_keys() in order; the moment a key returns a service
    overload (5xx/429) or transient network error, the SAME request is retried on the
    next key — so one busy/throttled key (Gemini's "high demand" 503) doesn't sink the
    inspection. A non-transient error (bad request, auth) propagates immediately. With
    a single key this is one attempt (fail fast). Raises the last error if every key is
    exhausted; the caller then shows the "overloaded" message.
    """
    keys = get_api_keys()
    if not keys:
        raise ValueError("No Gemini API key set (GEMINI_API_KEYS or GEMINI_API_KEY)")
    last_exc: Exception | None = None
    for i, key in enumerate(keys):
        try:
            return await asyncio.to_thread(fn, *args, api_key=key, **kwargs)
        except Exception as e:
            if not (isinstance(e, _TRANSIENT_NET_ERRORS) or _is_service_overload(e)):
                raise
            last_exc = e
            if i + 1 < len(keys):
                logging.warning(f"Gemini overload/transient on key #{i + 1}/{len(keys)} ({type(e).__name__}); failing over to next key")
            else:
                logging.warning(f"Gemini overload/transient on key #{i + 1}/{len(keys)} ({type(e).__name__}); no keys left")
    raise last_exc


async def _run_tire_pass(all_images) -> dict | None:
    """Best-effort focused tire-only pass. Never raises — it is a safety net, so a
    failure (API error, bad JSON) must not sink the inspection; we just skip the
    merge and keep the broad pass's result. Returns the parsed tire dict or None."""
    try:
        resp = await _call_gemini_with_retry(call_gemini_tires, all_images)
        td = parse_result(resp)
        return td if isinstance(td, dict) else None
    except Exception:
        logging.warning("Tire-only pass failed; continuing with the main result only", exc_info=True)
        return None


async def _run_split_passes(all_images: list, keys: list[str], history: list[dict] | None) -> dict:
    """Split frames across keys and analyze the chunks in parallel, one key each, then
    merge into a single result dict (see merge_frame_passes).

    Each chunk gets its own API key, so the load is spread across keys/projects. A
    chunk that overloads (503/429/transient) or returns unparseable JSON is dropped and
    we proceed on the survivors — a partial inspection beats no inspection. If a chunk
    fails with a non-overload error (bad request/auth) that's a real bug, so it
    propagates. If EVERY chunk fails we re-raise an overload so the caller shows the
    friendly 'overloaded' message.
    """
    chunks = _split_strided(all_images, len(keys))
    logging.info(
        f"PTI split: {len(all_images)} image(s) strided across {len(chunks)} key(s) "
        f"({', '.join(str(len(c)) for c in chunks)} per key)"
    )
    results = await asyncio.gather(
        *[asyncio.to_thread(call_gemini_photos, chunk, history=history, api_key=key)
          for chunk, key in zip(chunks, keys)],
        return_exceptions=True,
    )

    passes: list[dict] = []
    overload_exc: Exception | None = None
    for r in results:
        if isinstance(r, Exception):
            if not (isinstance(r, _TRANSIENT_NET_ERRORS) or _is_service_overload(r)):
                raise r
            overload_exc = r
            continue
        try:
            passes.append(parse_result(r))
        except Exception:
            logging.warning("Split chunk returned unusable JSON; dropping it", exc_info=True)

    if not passes:
        raise overload_exc or ValueError("All split passes returned unusable responses")
    if len(passes) < len(chunks):
        logging.warning(
            f"Split: only {len(passes)}/{len(chunks)} chunks succeeded; merging survivors "
            f"(some areas may show as not-filmed)"
        )
    return merge_frame_passes(passes)


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


_TIMESTAMP_RE = re.compile(r"^(\(\d+:\d{2}\))\s*")


def _split_timestamp(text: str) -> tuple[str, str]:
    """Split a leading "(M:SS)" video-moment marker off an issue text.

    Returns ``(timestamp, rest)`` where ``timestamp`` is "(M:SS)" (or "" if the
    text has none) and ``rest`` is the remaining defect text. Used so the marker
    can be rendered plain (un-bold) while the defect itself is emphasised.
    """
    m = _TIMESTAMP_RE.match(text)
    if m:
        return m.group(1), text[m.end():]
    return "", text


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


# The 8 REQUIRED PTI inspection areas, by their canonical "checked_clean" labels.
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
    "Windshield",
    "Air lines",
    "Frame",
    "ABS lamp",
)
# OPTIONAL areas are inspected and shown in the checklist, but NOT filming them never
# fails the inspection — they are deliberately kept out of REQUIRED_AREAS so they do
# not gate completeness. "Under hood" is inspected for leaks (a fuel leak is still
# reported as an OOS issue) but the engine bay is awkward/unsafe to film on every trip.
OPTIONAL_AREAS = (
    "Under hood",
)
# Order shown in the "Components inspected" checklist: required first, then optional.
CHECKLIST_AREAS = REQUIRED_AREAS + OPTIONAL_AREAS
_REQUIRED_AREAS_BY_KEY = {a.lower(): a for a in REQUIRED_AREAS}
_OPTIONAL_AREAS_BY_KEY = {a.lower(): a for a in OPTIONAL_AREAS}


def _missing_required_areas(data: dict) -> list[str]:
    """Required inspection areas the driver did not adequately film.

    Reads the model's structured ``missing_areas`` field, restricted to the known
    8-area vocabulary (so free-text noise can't trip the verdict) and de-duped
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
        # format_result adds the "re-film" prompt from missing_areas, so an incomplete
        # PTI needs no model advice — EXCEPT keep a "do not drive" when there's also an
        # OOS defect, since the verdict stays PASS-on-OOS and that line is the only
        # strong signal the driver gets.
        if not has_oos_defect(data):
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


# Cap how many worn-tire findings the focused pass can add, so a sensitive pass can
# never flood the report (drivers want the worn tire surfaced, not a wall of tires).
_MAX_TIRE_PROMOTE = 3


def merge_tire_pass(data: dict, tire_data: dict | None) -> int:
    """Fold a focused tire-only pass (utils.gemini.call_gemini_tires) into the main result.

    The broad PTI pass juggles 8 areas over 150+ frames and reliably overlooks a
    single worn tire (attention dilution); a narrow tire-only pass over the same
    frames catches it. The driver MUST see a clearly worn-out tire so they replace
    it — but per company policy worn/bald tread is an ADVISORY, never out-of-service,
    so every promoted finding is forced to oos=false. We promote only when the broad
    pass didn't already flag a tire, and cap the count, so the more-sensitive pass
    can't flood the report. Promoted issues are appended BEFORE
    filter_hallucinated_issues so they pass the same evidence gate. Returns how many
    issues were promoted.
    """
    if not tire_data or not tire_data.get("tire_defect"):
        return 0
    # Don't double-report if the broad pass already mentioned a tire/dual.
    if any("tire" in _issue_text(i).lower() or "dual" in _issue_text(i).lower()
           for i in (data.get("issues") or [])):
        return 0
    promote = [
        i for i in (tire_data.get("issues") or [])
        if _issue_text(i) and len(_issue_evidence(i)) >= _MIN_EVIDENCE_CHARS
    ][:_MAX_TIRE_PROMOTE]
    if not promote:
        return 0
    for issue in promote:
        # Company policy: tire tread wear is advisory, not OOS — enforce it here even
        # if the model labelled it otherwise.
        issue["oos"] = False
    data.setdefault("issues", []).extend(promote)
    # A reported tire defect means tires WERE filmed: drop "Tires" from clean and
    # from any missing/not-visible list so the area is accounted for by the issue
    # alone (see _missing_required_areas) — no contradictory "clean"/"missing" Tires.
    data["checked_clean"] = [c for c in (data.get("checked_clean") or [])
                             if str(c).strip().lower() != "tires"]
    for key in ("missing_areas", "what_was_not_visible"):
        vals = data.get(key)
        if vals:
            data[key] = [v for v in vals if str(v).strip().lower() != "tires"]
    return len(promote)


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
        # Match banned phrases against the EVIDENCE only, not the title. A concrete
        # finding's short title naturally uses words like "worn trailer tire", which
        # would wrongly nuke it if matched — the gate's job is to reject vague
        # *evidence*, so judge the evidence. (Vague phrasing in evidence still drops.)
        matched = next((p for p in _BANNED_EVIDENCE_PHRASES if p in evidence.lower()), None)
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


def merge_frame_passes(passes: list[dict]) -> dict:
    """Combine the per-chunk results of a split-frame inspection into one data dict.

    When frames are split across keys (see _run_split_passes), each chunk pass sees
    only a SLICE of the walkaround, so its own completeness verdict is meaningless on
    its own. We rebuild a single result and let finalize_result set the verdict:
      - checked_clean: UNION — an area filmed-clean in ANY chunk was filmed and is fine.
      - issues: concatenated — a defect seen in any chunk is real (dedup/filter later).
      - missing_areas: INTERSECTION — an area is only "not filmed" if EVERY chunk that
        judged it agrees it's missing; the chunk that actually saw the area (clean or
        as an issue) won't list it missing, so it drops out of the intersection. This
        is what stops a perfectly-filmed truck from FAILing just because no single
        70-frame slice contained all 8 areas.
    status/severity/advice are intentionally omitted — finalize_result recomputes them.
    """
    passes = [p for p in passes if isinstance(p, dict)]
    clean: list[str] = []
    seen: set[str] = set()
    issues: list = []
    missing_sets: list[set[str]] = []
    not_visible_sets: list[set[str]] = []
    fire: bool | None = None
    confidences: list[str] = []
    qualities: list[str] = []
    for p in passes:
        for c in p.get("checked_clean") or []:
            k = str(c).strip().lower()
            if k and k not in seen:
                seen.add(k)
                clean.append(str(c).strip())
        issues.extend(p.get("issues") or [])
        missing_sets.append({str(a).strip().lower() for a in (p.get("missing_areas") or [])})
        not_visible_sets.append({str(a).strip().lower() for a in (p.get("what_was_not_visible") or [])})
        fe = p.get("fire_extinguisher_shown")
        if fe is True:
            fire = True
        elif fe is False and fire is None:
            fire = False
        if p.get("confidence"):
            confidences.append(p["confidence"])
        if p.get("image_quality"):
            qualities.append(p["image_quality"])

    common_missing = set.intersection(*missing_sets) if missing_sets else set()
    common_not_visible = set.intersection(*not_visible_sets) if not_visible_sets else set()
    clean_keys = {c.lower() for c in clean}
    missing = [_REQUIRED_AREAS_BY_KEY[k] for k in _REQUIRED_AREAS_BY_KEY
               if k in common_missing and k not in clean_keys]

    return {
        "checked_clean": clean,
        "issues": issues,
        "missing_areas": missing,
        "what_was_not_visible": sorted(common_not_visible - clean_keys),
        "fire_extinguisher_shown": fire,
        # Cosmetic meta — keep the first reported value; the verdict doesn't depend on it.
        "confidence": confidences[0] if confidences else "",
        "image_quality": qualities[0] if qualities else "",
    }


def finalize_result(data: dict, tire_data: dict | None = None) -> int:
    """Assemble the final result from the broad pass + optional tire-only pass.

    Order matters: filter the broad pass's issues FIRST, so a vague broad-pass tire
    issue (which the gate drops) can't trip merge_tire_pass's no-double-report guard
    and suppress a real tire-pass finding. Then merge the worn-tire findings, filter
    AGAIN so the promoted issues face the same evidence gate, and finally set the
    completeness verdict. Returns the number of tire findings promoted.
    """
    filter_hallucinated_issues(data)
    promoted = merge_tire_pass(data, tire_data)
    if promoted:
        filter_hallucinated_issues(data)
    # PASS/FAIL depends ONLY on completeness; OOS/advisory defects are reported but
    # never fail the inspection.
    apply_completeness_verdict(data)
    return promoted


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

    clean_keys = {str(c).strip().lower() for c in clean}
    missing_keys = {str(a).strip().lower() for a in missing_areas}
    oos_issues = [t for t in (_issue_text(i) for i in issues if _issue_is_oos(i)) if t]
    advisory_issues = [t for t in (_issue_text(i) for i in issues if not _issue_is_oos(i)) if t]

    # One flat inspection list: defects first (❌ out-of-service, then ⚠️ advisory),
    # then the components verified clean (✅). Un-filmed areas are NOT shown here —
    # they go in the "Not visible" line below — keeping the list short and scannable.
    # The leading "(M:SS)" video-moment marker stays plain; only the defect text is
    # bold. OOS defects get a trailing "🚫 OOS" tag so the driver sees the severity.
    body: list[str] = []
    for t in oos_issues:
        ts, rest = _split_timestamp(t)
        prefix = f"{escape(ts)} " if ts else ""
        body.append(f"❌ {prefix}<b>{escape(rest)}</b> 🚫 OOS")
    body.extend(f"⚠️ {escape(t)}" for t in advisory_issues)
    body.extend(f"✅ {escape(area)}" for area in CHECKLIST_AREAS if area.lower() in clean_keys)
    if body:
        lines.append("")
        lines.extend(body)

    # Everything not filmed / not confirmed, in one line: required areas the driver
    # skipped (the FAIL reason for an incomplete PTI) plus any free-text notes, de-duped.
    not_seen: list[str] = list(missing_areas)
    seen = set(missing_keys)
    for n in not_visible:
        key = str(n).strip().lower()
        if key and key not in seen:
            seen.add(key)
            not_seen.append(str(n))
    if not_seen:
        lines.append("")
        lines.append(f"👁 <b>Not visible:</b> {escape(', '.join(not_seen))}")

    # One advice line. Keep the model's advice (e.g. "Do not drive…") AND, when footage
    # is missing, the re-film prompt — so an incomplete + out-of-service result shows both.
    advice_parts: list[str] = []
    if advice:
        advice_parts.append(advice)
    if missing_areas and not any("film" in p.lower() for p in advice_parts):
        advice_parts.append("Re-film the missing areas to finish the PTI.")
    if advice_parts:
        lines.append("")
        lines.append(f"💡 {escape(' '.join(advice_parts))}")

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
    tire_task: asyncio.Task | None = None
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
        # Decide the broad PTI pass: either one whole-footage call, or — when several
        # API keys are configured and there are enough frames — split the frames across
        # the keys and analyze the chunks in parallel (_run_split_passes merges them).
        # The tire-only pass is a best-effort safety net for worn tires the broad pass
        # overlooks; it runs over ALL frames and never raises, so only a broad-pass
        # failure here propagates to the error handling below.
        keys = get_api_keys()
        use_split = _should_split(len(all_images), len(keys))
        broad_coro = (
            _run_split_passes(all_images, keys, history) if use_split
            else _call_gemini_with_retry(call_gemini_photos, all_images, history=history)
        )

        # The tire pass runs as a named task rather than in a gather() with the broad
        # pass: gather propagates a broad-pass failure immediately WITHOUT waiting for
        # the tire task, which then kept reading the frame files after the finally
        # block below deleted them (FileNotFoundError mid-upload). The finally block
        # instead waits for this task before removing the frames.
        if config.PTI_TIRE_PASS:
            tire_task = asyncio.create_task(_run_tire_pass(all_images))
        broad_result = await broad_coro
        tire_data = (await tire_task) if tire_task else None
        # Split passes return an already-merged data dict; the single call returns a
        # raw Gemini response that still needs parsing.
        response = None if use_split else broad_result

        try:
            data = broad_result if use_split else parse_result(response)
            promoted = finalize_result(data, tire_data)
            if promoted:
                logging.info(f"Tire pass promoted {promoted} worn-tire advisory(ies) the broad pass missed")
            text = format_result(data, photos=photo_count, videos=video_count, driver_name=driver_name)
        except (json.JSONDecodeError, KeyError):
            data = {}
            text = f"<b>PTI Result:</b>\n{response.text if response else ''}"

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
        if tire_task is not None and not tire_task.done():
            # The broad pass failed while the tire pass was still uploading the frame
            # files. Its worker thread can't be cancelled, so wait it out (it never
            # raises) before deleting the frames it is reading.
            await tire_task
        if video_frames:
            delete_frames(video_frames)
        for path in tmp_paths:
            try:
                os.remove(path)
            except OSError:
                pass
