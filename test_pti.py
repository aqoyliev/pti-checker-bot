from __future__ import annotations

import glob
import sys
import os
import json
import logging
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types

load_dotenv()

PTI_FRAME_INTERVAL = float(os.getenv("PTI_FRAME_INTERVAL", "1"))
MAX_VIDEO_DURATION = 900   # 15 minutes — reject anything longer
MAX_FRAMES = 900           # safety cap so we never exceed Gemini's token limit
FILE_API_THRESHOLD = 50    # frames above this count are uploaded via File API instead of sent inline


class VideoTooLongError(Exception):
    def __init__(self, duration: float):
        self.duration = duration
        super().__init__(f"Video duration {duration:.0f}s exceeds the {MAX_VIDEO_DURATION}s limit")


def _upload_one(client, path: str, mime_type: str, label: str):
    return client.files.upload(
        file=path,
        config=genai_types.UploadFileConfig(mime_type=mime_type, display_name=label[:40]),
    )


def _delete_files_background(client, files: list):
    """Delete uploaded files in parallel without blocking the caller."""
    def _del(uf):
        try:
            client.files.delete(name=uf.name)
        except Exception:
            logging.warning(f"Failed to delete uploaded file {uf.name}", exc_info=True)

    pool = ThreadPoolExecutor(max_workers=16)
    for uf in files:
        if uf is not None:
            pool.submit(_del, uf)
    pool.shutdown(wait=False)

SYSTEM_PROMPT = """You are an experienced commercial-truck inspector. \
Analyze the supplied frames/photos from a pre-trip inspection (PTI) of a semi-truck and/or trailer as one combined inspection.

Inspection scope — check ONLY these 9 areas. Do not flag anything outside this list:
  1. Brake pads — truck and trailer (look for severely worn pads, missing pads, broken hardware)
  2. Lights — truck and trailer (headlights, marker, turn, brake, tail; flag only clearly broken / missing / non-functional)
  3. Tires — tread depth and air pressure (truck and trailer). EVERY tire position must be filmed — see the tire-completeness rule below.
  4. Side mirrors — 4 total, 2 on the hood (flag only clearly broken / missing / cracked)
  5. Under the hood — engine oil level and visible leaks
  6. Windshield (flag only clear cracks/chips in the driver's view)
  7. Air lines (flag only visible cuts, leaks, or disconnected lines)
  8. Overall frame / chassis (flag only clearly cracked, bent, or damaged frame members)
  9. ABS malfunction lamp (trailer) — the warning indicator next to a stamped "ABS" label. It MUST be filmed (it is normally OFF). See the ABS rule below for how to judge on/off and where to place it.

Fire extinguisher and warning triangle (presence) — track this SEPARATELY from the 9 required areas above; it does NOT affect completeness or PASS/FAIL. Set "fire_extinguisher_shown": true if that storage area appears in any frame (regardless of whether the extinguisher/triangle itself is present), or false if it was never filmed at all. If the area IS visible and you can clearly confirm the extinguisher or triangle is actually missing, you may still report that as an advisory issue (oos=false, see the NEVER OOS list below) — that is independent of "fire_extinguisher_shown".

Leniency rules — BE CONSERVATIVE. The default is PASS. It is FAR worse to falsely flag a good component than to miss a borderline defect. Drivers lose trust in this bot when it invents issues. When in doubt: PASS. Never INVENT a defect you cannot describe precisely (size, shape, location, what makes you certain).

  - **Tires — be extra strict before flagging. Past false positives have happened on completely good tires.**
    - **Tread depth / wear — observations only, never conclusions.** You CANNOT measure tread depth from a video or photo, so NEVER quote a number ("below 4/32", "2/32 left") or invoke "wear bars". ALSO BANNED — these are conclusions, not observations, and you must NEVER use them: "severe wear", "heavy wear", "tires show wear", "worn tires", "replace soon", "tread is low", or any similar vague summary.
      **The outer SHOULDER of a tire — the curved edge where the tread meets the sidewall — is naturally smoother and less aggressively treaded than the center tread by design.** That is normal tire anatomy on every commercial tire and NEVER a defect. Do NOT flag "shoulder is smooth", "shoulder bald", or "outer edge worn" — those are not real issues. Comparing the center of a tire to its own shoulder is invalid because the two areas are SUPPOSED to look different.
      You MAY flag wear ONLY when you see a localized bald/smooth patch on the CENTER tread (not the shoulder) of ONE dual tire that the ADJACENT dual tire clearly does NOT have. Your issue text MUST include both (a) which dual (inner vs outer) and (b) the contrast specifically against the OTHER dual visible in the same frame. Same-tire comparisons (center vs shoulder, left vs right side of one tire) do NOT count as evidence. When in doubt, PASS and add "Tires" to "checked_clean".
      **Mixed tread patterns are NOT a defect.** Trucks often run two different tire models on the same dual axle (e.g., a block-pattern tread next to a lug-pattern tread) because one tire was replaced and the other wasn't. Different brand, different tread design, different wear age — all NORMAL. Do NOT flag tires just because the two duals look different from each other. The contrast that matters for wear is "smooth/bald patch on one vs grooved tread on the other", NOT "different shaped tread blocks".
      Valid example: "(1:15) Inner dual smooth in center tread vs grooved outer dual (49 CFR 393.75)".
      INVALID — do NOT produce these: "Drive tires show severe visible wear", "Tire heavily worn", "Tread is low", "Outer shoulder is smooth/bald", "Shoulder bald".
    - "Cracked sidewall" requires a visible split in the rubber where you can plainly see a gap or separation in the surface. Weathered-looking rubber, normal sidewall lettering/DOT codes, brand markings, mold lines, shadows, dirt, mud, or texture variation are NOT cracks. If you have to squint, it is not a crack.
    - "Cut sidewall" requires a visible incision — a clean line where the rubber is severed. Scuff marks, paint, mud, shadows, stickers, and brand stamps are NOT cuts.
    - "Bulge" requires a clearly protruding section, not normal sidewall curvature.
    - **Puncture / hole / embedded object — this is the highest false-positive risk area, treat it with extra care.** Do NOT flag a puncture unless ALL of these are true:
        (1) the dark spot is on the FACE of a tread block (the raised rubber pad), NOT inside a groove between blocks;
        (2) you can clearly see it is recessed INTO the rubber (a cavity), not something sitting on top of the rubber;
        (3) it is darker than the surrounding rubber — pebbles, stones, and road debris look LIGHT (white / grey / tan) and reflective, and are NEVER punctures even when they look embedded;
        (4) it is not part of a splash, mud spot, water droplet, paint, road grime, or wet-surface texture pattern.
      If the tire is wet, freshly washed, or has visible mud/grime, raise the bar further — most "dark spots" on a wet tread block are water or mud, not punctures. When in doubt, PASS.
      If you DO flag, your issue text MUST describe (a) which tread block (e.g., "rightmost outer block of the inner dual"), (b) the cavity's apparent shape and depth, and (c) what made you certain it is not a pebble or mud spot. Format: "(M:SS) Puncture in trailer tire tread (49 CFR 393.75)".
    - A tire is FINE unless you can clearly see one of: visibly flat (deformed against the ground), exposed cord/belt fabric, an unmistakable sidewall split or incision, a clear bulge, a puncture/hole/embedded object in the tread or sidewall, or clearly visible bald/smooth patches where the tread pattern is missing or severely faded compared to the rest of the tire or the adjacent dual. Otherwise treat it as PASS — but only add "Tires" to "checked_clean" if EVERY tire position was shown (see tire-completeness below).
    - TIRE COMPLETENESS — every tire must be filmed. Add "Tires" to "checked_clean" ONLY when the footage shows EVERY wheel position on the truck and trailer: both steer tires, all drive tires (both sides of each drive axle, including the inner and outer duals), and all trailer tires (both sides, inner and outer duals). If any wheel position is never shown — e.g. only one side of the vehicle is filmed, the inner dual is never visible, or the trailer tires are skipped — do NOT put "Tires" in "checked_clean". Put "Tires" in "missing_areas" instead, so the inspection is INCOMPLETE and FAILs and the driver re-films all tires. When you cannot confirm every tire was shown, treat "Tires" as missing, not clean.
  - Air bags / air suspension bellows ("balloons"): these are NOT in scope. Do not flag them. A bag that looks intact is fine.
  - Lights: only flag if a light is obviously broken (visible shattered lens), missing from its housing, or clearly not illuminating when others around it are. Don't fail on dirt, reflections, or being turned off.
  - **ABS malfunction lamp — this is a WARNING INDICATOR, so the logic is INVERTED from exterior lamps: here illuminated = DEFECT, off = good.** Trailers carry an external ABS malfunction lamp, identified by a stamped/painted "ABS" label right next to it. This lamp is OFF almost all the time — treat OFF as the strong default and only flag it ON with high certainty. Flag it ONLY when ALL of these hold: (a) a legible "ABS" text label is clearly visible; (b) the lamp immediately next to that label is genuinely SELF-ILLUMINATED — it emits light, i.e. a bright glow/bloom or halo clearly brighter than its surroundings that washes out the lens detail; AND (c) it reads as lit in at least TWO separate frames (a one-frame flash is a reflection, not a warning lamp). A merely colored, red, amber, dark, or glossy lens is OFF — warning lenses look colored even when unlit, so color is NOT evidence of illumination. A reflection, sun glint, headlight/flash bounce, wet-surface sparkle, or a single bright pixel is NOT illumination. If you cannot read an "ABS" label, do NOT flag — a lit marker lamp, brake lamp, red reflector, or glint is NOT an ABS warning. If you are not certain the lamp is actively emitting light, treat it as OFF and do NOT flag (default to PASS). An illuminated ABS lamp IS out-of-service (see below): report it with oos=true. Format: "(M:SS) ABS malfunction lamp on (49 CFR 393.55)". PLACEMENT — the ABS lamp is a required inspection area ("ABS lamp"): if it is visible and OFF, add "ABS lamp" to "checked_clean"; if it is visible and genuinely lit, report it as an issue (do NOT also list it in checked_clean); if the ABS lamp / "ABS" label was NEVER filmed, put "ABS lamp" in "missing_areas" — an un-filmed ABS lamp makes the inspection INCOMPLETE and FAILs it.
  - Windshield: small stone chips outside the driver's line of sight are NOT a defect; only flag long/spreading cracks or chips in the swept area.
  - If you are unsure whether something is a defect, treat it as PASS and (if relevant) add it to "what_was_not_visible" rather than "issues".

PASS / FAIL rule — based ONLY on completeness, NOT on defects:
  The overall verdict is decided SOLELY by whether the driver filmed every required inspection area
  (see the Completeness rule below). It is NOT related to OOS or to any defect:
    - PASS = all 9 inspection areas were filmed — even if you found out-of-service or advisory defects.
    - FAIL = at least one required area was never filmed (an incomplete inspection).
  Defects NEVER change the verdict: a truck with an out-of-service defect still PASSES if every area was
  filmed, and a defect-free truck still FAILS if an area was not filmed. You still REPORT every defect you
  see (drivers must fix them) — you just don't fail the inspection over them.

"oos" labeling — for reporting only, does NOT affect PASS/FAIL:
  For EVERY issue you report, set "oos": true ONLY if that specific defect meets one of the OOS conditions
  below; otherwise set "oos": false. Only mark oos=true for something you can actually SEE meets the threshold.
  This labels the defect as out-of-service in the report so the driver knows its severity; it does NOT fail
  the inspection.

  OOS conditions (label these oos=true):
    - Tires: visibly flat / run-flat (deformed against the ground), tread or sidewall separation, a bulge from
      ply/belt separation, exposed cords/belt/ply fabric, a sidewall cut or split deep enough to expose cords,
      or a clearly bald patch where the tread is gone. (You cannot measure tread depth — never fail on a number.)
    - Brakes / brake pads: lining or pad missing, cracked off, or worn down to the metal/rivets; broken brake
      hardware. Even, adequate pad thickness is NOT OOS.
    - Air lines (air brake system): an air line that is cut, broken, disconnected, or visibly/audibly leaking.
    - Frame / chassis: a cracked, broken, or sagging frame member (surface rust or cosmetic dents are NOT OOS).
    - Under the hood: a FUEL leak is OOS. An engine-oil drip or seep is NOT OOS.
    - Lights: OOS only if a REQUIRED lamp is dead — e.g. no working brake (stop) lamps, or an inoperative
      headlamp or turn signal. A single out/dirty marker or clearance lamp is an advisory, not OOS.
    - ABS: an illuminated ABS malfunction lamp (warning indicator lit next to a legible "ABS" label) is OOS.

  NEVER OOS (always oos=false — label as an advisory, not out-of-service):
    - Missing or expired fire extinguisher or warning triangle (regulatory item, not an OOS condition).
    - Broken, missing, or cracked mirror.
    - Low or unknown engine-oil level.
    - Windshield stone chips or short cracks outside the swept driver view.
    - Cosmetic damage, dirt, rust, mud, faded paint.

Completeness rule — a PTI must actually SHOW all 9 inspection areas:
  Every one of the 9 areas must end up in exactly ONE of "checked_clean" (it appears in the
  footage and looks fine), "issues" (a defect you saw), or "missing_areas" (it NEVER appears
  in any frame). "missing_areas" means the AREA WAS NOT FILMED AT ALL — the camera was never
  pointed at it. It does NOT mean a fine detail was hard to judge: if the area shows up in even
  one frame, it counts as FILMED — put it in "checked_clean" (or "issues" if you saw a defect),
  even when you cannot confirm every sub-detail (e.g. a possible hairline windshield crack, the
  far-side mirror, or the engine-oil dipstick level). Record those un-assessable sub-details in
  "what_was_not_visible" instead — NEVER in "missing_areas". An inspection with a non-empty
  "missing_areas" is INCOMPLETE and will be marked FAIL so the driver re-records the un-filmed
  areas — this is independent of OOS status (an incomplete video fails even with zero defects).
  A driver must not be able to PASS by simply not filming a component. Be conservative: list an
  area as missing ONLY if it truly never appears in any frame.

Process:
  - Look across ALL frames before deciding — a defect visible in one frame is still a defect.
  - Severity: rates the worst defect found, for the driver's awareness — it is SEPARATE from PASS/FAIL. CRITICAL = at least one out-of-service defect; MAJOR/MINOR = advisory defects worth fixing; NONE = no defects. (PASS/FAIL still depends only on completeness.)
  - VEHICLE IDENTIFICATION: Read visible unit numbers (painted on cab or trailer body) and license plates for the truck and trailer separately. Return one entry per distinct vehicle in "vehicles". If previous PTI history is provided, check whether prior issues are now fixed.

Output rules — the driver reads this on a phone, so be brutally short:
  - Frames from a video are labeled "Video frame at M:SS". Plain photos are labeled "Photo N".
  - For any defect you spot in a video frame, prefix the issue with that frame's timestamp in parens, e.g. "(1:14)". If the same defect spans several frames, use the timestamp where it's clearest. For defects only seen in a Photo (not a video frame), do NOT add a timestamp prefix.
  - Each issue is an OBJECT with three fields: "text", "evidence", and "oos".
    "text" = ≤ 8 words plain English, then ONE CFR citation in parens. Format: "(M:SS) <short defect> (49 CFR <section>)" for video defects, or "<short defect> (49 CFR <section>)" for photo defects.
    Good text: "(1:14) Cracked steer rim (49 CFR 393.205(a))", "Trailer plate missing (49 CFR 393.17)".
    Bad text:  "Cracked rim on passenger side steer wheel (49 CFR 393.205(a), 396 Appendix G, Item 1.a.1)".
    If no clear CFR applies (e.g. minor cosmetic damage), omit the CFR parens entirely (but keep the timestamp).
    "evidence" = a sentence (20–200 chars) describing WHAT YOU ACTUALLY SAW on the frame — specific location, shape, size, contrast. Do NOT restate the defect; describe the visual observation that proves it. If you cannot write a concrete evidence sentence with specific location and visual contrast, DO NOT include the issue.
    Good evidence: "Round dark cavity ~1cm wide on rightmost tread block of inner dual, recessed below surrounding rubber".
    Bad evidence (DO NOT produce): "Tire is worn", "Visible damage", "Severe wear visible", "Tread depth low", "Shoulder is smooth".
    "oos" = true/false per the OOS conditions above — true ONLY if this defect places the vehicle out of service, otherwise false.
  - "checked_clean": list which of the 9 inspection areas you actually saw and verified are fine.
    Use ONLY these component labels (one per inspection area):
      "Brake pads", "Lights", "Tires", "Mirrors",
      "Under hood", "Windshield", "Air lines", "Frame", "ABS lamp".
    Each entry is just the component name — no timestamps, no extra text.
    Example: ["Tires", "Mirrors", "Lights", "Windshield"].
    Omit any component you couldn't see clearly. Don't put a component in both "issues" and "checked_clean".
  - "missing_areas": of the 9 inspection areas, ONLY those the driver never filmed at all (the area
    does not appear in a single frame). If an area shows up in even one frame, it is NOT missing —
    put it in "checked_clean" or "issues", never here, even if a fine detail was unclear.
    Use ONLY the same component labels as "checked_clean" (one per area):
      "Brake pads", "Lights", "Tires", "Mirrors",
      "Under hood", "Windshield", "Air lines", "Frame", "ABS lamp".
    An area goes here ONLY if it is not in "checked_clean" and not covered by an "issue". Empty list
    means the inspection was complete. This drives the INCOMPLETE → FAIL rule above, so be accurate.
  - "what_was_not_visible": at most 5 short items, only the most important ones. Don't list every PTI area you didn't see — just the ones a driver could reasonably re-shoot.
    Describe the specific un-assessable detail (e.g. "Engine-oil dipstick level", "Passenger-side mirror glass", "Windshield crack detail") — NEVER a bare inspection-area label like "Under hood" or "Windshield" on its own (a bare label means the whole area wasn't filmed, which belongs in "missing_areas").
    DO NOT include "Trailer license plate" or "Trailer unit number" — drivers are not required to film these.
  - DO NOT list trailer license plate or trailer unit number absence as an issue. Drivers are not required to show them. (You may still fill them in the "vehicles" section if they happen to be visible.)
  - "advice": ONE short sentence (≤ 15 words) with the action to take. No regulations.
  - "confidence": "HIGH" if frames are clear and defects are obvious; "MEDIUM" if some calls are borderline; "LOW" if the video is dark/blurry/short.
  - "image_quality": "GOOD" / "FAIR" / "POOR" based on lighting, focus, framing.

Respond ONLY with a raw JSON object — no markdown, no code fences:
{
  "status": "PASS" or "FAIL",
  "severity": "NONE" or "MINOR" or "MAJOR" or "CRITICAL",
  "confidence": "HIGH" or "MEDIUM" or "LOW",
  "image_quality": "GOOD" or "FAIR" or "POOR",
  "issues": [
    {
      "text": "(M:SS) short defect (49 CFR ...)",
      "evidence": "what you actually saw — location, shape, contrast, 20-200 chars",
      "oos": true or false
    }
  ],
  "checked_clean": ["Tires", "Lights", ...],
  "missing_areas": ["Brake pads", ...],
  "fire_extinguisher_shown": true or false,
  "what_was_not_visible": ["short item", ...],
  "advice": "one short sentence",
  "vehicles": [
    {
      "type": "truck" or "trailer",
      "unit_number": "visible unit number or null",
      "plate": "visible license plate or null"
    }
  ]
}"""


def _build_history_text(history: list[dict]) -> str | None:
    if not history:
        return None
    lines = ["Previous PTI submissions for this driver (most recent first):"]
    for i, entry in enumerate(history, 1):
        status = "PASS" if entry.get("passed") else "FAIL"
        severity = entry.get("severity", "?")
        submitted_at = entry.get("submitted_at", "?")
        date = submitted_at.strftime("%Y-%m-%d") if hasattr(submitted_at, "strftime") else str(submitted_at)[:10]
        unit = entry.get("unit_number") or "unknown unit"
        result = entry.get("result_json")
        issues = []
        if result:
            try:
                parsed = json.loads(result)
                issues = parsed.get("issues", [])
            except Exception:
                pass
        issue_texts = [
            (i.get("text") or "").strip() if isinstance(i, dict) else str(i).strip()
            for i in issues
        ]
        issue_texts = [t for t in issue_texts if t]
        issue_text = "; ".join(issue_texts) if issue_texts else "none"
        lines.append(f"  {i}. {date} — {status} ({severity}) on {unit}. Issues: {issue_text}")
    return "\n".join(lines)


def _probe_duration(video_path: str) -> float | None:
    """Return video duration in seconds via ffprobe, or None if it can't be read."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        raise RuntimeError("ffprobe not found on PATH (ffmpeg must be installed)")
    try:
        return float((proc.stdout or "").strip())
    except ValueError:
        logging.warning(
            f"ffprobe could not read duration for {os.path.basename(video_path)}: "
            f"{proc.stderr.strip()[:200]}"
        )
        return None


def extract_frames(video_path: str, interval_seconds: float = PTI_FRAME_INTERVAL) -> list[tuple[float, str]]:
    """Sample one JPEG frame every ``interval_seconds`` using ffmpeg.

    Returns ``[(timestamp_seconds, jpg_path), ...]``. ffmpeg decodes the stream
    once sequentially (far faster than per-frame seeking), caps the longest side
    at 1280px (never upscaling), and JPEG-encodes in one pass. Raises
    ``VideoTooLongError`` if the clip exceeds ``MAX_VIDEO_DURATION``.
    """
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video file not found: {video_path}")

    duration = _probe_duration(video_path)
    if duration is not None and duration > MAX_VIDEO_DURATION:
        raise VideoTooLongError(duration)

    temp_dir = tempfile.mkdtemp(prefix="pti_frames_")
    pattern = os.path.join(temp_dir, "frame_%05d.jpg")
    fps = 1.0 / interval_seconds
    vf = (
        f"fps={fps:g},"
        "scale='min(1280,iw)':'min(1280,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2"
    )
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
        "-i", video_path,
        "-vf", vf,
        "-frames:v", str(MAX_FRAMES),
        "-q:v", "2",
        pattern,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise RuntimeError("ffmpeg not found on PATH (ffmpeg must be installed)")
    if proc.returncode != 0:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise RuntimeError(f"ffmpeg frame extraction failed: {proc.stderr.strip()[:300]}")

    files = sorted(glob.glob(os.path.join(temp_dir, "frame_*.jpg")))
    if not files:
        shutil.rmtree(temp_dir, ignore_errors=True)
        logging.warning(f"ffmpeg produced no frames for {os.path.basename(video_path)}")
        return []

    saved = [(i * interval_seconds, path) for i, path in enumerate(files)]
    dur_text = f"{duration:.1f}s" if duration is not None else "unknown duration"
    logging.info(
        f"Extracted {len(saved)} frames from {os.path.basename(video_path)} "
        f"({dur_text} @ {interval_seconds}s interval)"
    )
    return saved


def call_gemini(frames: list[tuple[float, str]], history: list[dict] | None = None):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "your-gemini-key-here":
        raise ValueError("GEMINI_API_KEY is not set in .env")

    client = genai.Client(api_key=api_key)
    n = len(frames)
    use_file_api = n > FILE_API_THRESHOLD
    uploaded_files = []

    try:
        parts = []
        if use_file_api:
            logging.info(f"Uploading {n} frames via File API (parallel)...")
            frame_tuples = [("image/jpeg", path, f"Frame {i + 1} of {n}") for i, (_, path) in enumerate(frames)]
            with ThreadPoolExecutor(max_workers=8) as pool:
                futs = {pool.submit(_upload_one, client, path, mime, label): idx
                        for idx, (mime, path, label) in enumerate(frame_tuples)}
                results = [None] * n
                for fut in as_completed(futs):
                    results[futs[fut]] = fut.result()
            uploaded_files = results
            for i, uf in enumerate(uploaded_files):
                parts.append(genai_types.Part.from_uri(file_uri=uf.uri, mime_type="image/jpeg"))
                parts.append(f"Frame {i + 1} of {n}")
        else:
            for i, (_, path) in enumerate(frames):
                with open(path, "rb") as f:
                    parts.append(genai_types.Part.from_bytes(data=f.read(), mime_type="image/jpeg"))
                parts.append(f"Frame {i + 1} of {n}")

        history_text = _build_history_text(history or [])
        if history_text:
            parts.append(history_text)
        parts.append(f"Analyze all {n} frames above as a single PTI inspection and return the JSON result.")

        response = client.models.generate_content(
            model="gemini-2.5-pro",
            config=genai_types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0,
                response_mime_type="application/json",
            ),
            contents=parts,
        )
        return response
    finally:
        if uploaded_files:
            _delete_files_background(client, uploaded_files)


def call_gemini_photos(images: list[tuple], history: list[dict] | None = None):
    """images: list of (file_path, mime_type) or (file_path, mime_type, label).
    Label is shown to Gemini after each image — e.g. "Video frame at 1:14" or "Photo 2".
    Above FILE_API_THRESHOLD images, files are uploaded via the File API instead of sent inline.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "your-gemini-key-here":
        raise ValueError("GEMINI_API_KEY is not set in .env")

    client = genai.Client(api_key=api_key)
    n = len(images)
    use_file_api = n > FILE_API_THRESHOLD
    uploaded_files = []

    # Normalise to (path, mime_type, label) triples
    triples = []
    for i, item in enumerate(images):
        if len(item) == 3:
            triples.append(item)
        else:
            path, mime_type = item
            triples.append((path, mime_type, f"Photo {i + 1} of {n}"))

    try:
        parts = []
        if use_file_api:
            logging.info(f"Uploading {n} images via File API (parallel)...")
            with ThreadPoolExecutor(max_workers=8) as pool:
                futs = {pool.submit(_upload_one, client, path, mime, label): idx
                        for idx, (path, mime, label) in enumerate(triples)}
                results = [None] * n
                for fut in as_completed(futs):
                    results[futs[fut]] = fut.result()
            uploaded_files = results
            for uf, (_, mime_type, label) in zip(uploaded_files, triples):
                parts.append(genai_types.Part.from_uri(file_uri=uf.uri, mime_type=mime_type))
                parts.append(label)
        else:
            for path, mime_type, label in triples:
                with open(path, "rb") as f:
                    parts.append(genai_types.Part.from_bytes(data=f.read(), mime_type=mime_type))
                parts.append(label)

        history_text = _build_history_text(history or [])
        if history_text:
            parts.append(history_text)
        parts.append(f"Analyze all {n} image(s) above as a single PTI inspection and return the JSON result.")

        response = client.models.generate_content(
            model="gemini-2.5-pro",
            config=genai_types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0,
                response_mime_type="application/json",
            ),
            contents=parts,
        )
        return response
    finally:
        if uploaded_files:
            _delete_files_background(client, uploaded_files)


def delete_frames(frames: list[tuple[float, str]]) -> None:
    for _, path in frames:
        try:
            os.remove(path)
        except OSError:
            pass
    if frames:
        try:
            os.rmdir(os.path.dirname(frames[0][1]))
        except OSError:
            pass


def parse_result(response) -> dict:
    text = getattr(response, "text", None)
    if not text:
        reason = "unknown"
        try:
            reason = str(response.candidates[0].finish_reason)
        except Exception:
            pass
        raise ValueError(f"Gemini returned no text (finish_reason={reason})")
    return json.loads(text)


def print_result(response) -> None:
    print("\n--- RESULT ---")
    try:
        parsed = parse_result(response)
        print(json.dumps(parsed, indent=2))
    except json.JSONDecodeError:
        print("Warning: Response is not valid JSON. Raw output:")
        print(response.text)

    usage = response.usage_metadata
    print("\n--- USAGE ---")
    print(f"  Input tokens:  {usage.prompt_token_count}")
    print(f"  Output tokens: {usage.candidates_token_count}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python test_pti.py <path_to_video>")
        sys.exit(1)

    video_path = sys.argv[1]

    print(f"Extracting frames every {PTI_FRAME_INTERVAL}s from: {video_path}")
    frames = extract_frames(video_path)

    if not frames:
        print("Error: No frames could be extracted from the video.")
        sys.exit(1)

    print(f"\nSending {len(frames)} frames to Gemini...")
    try:
        response = call_gemini(frames)
    finally:
        delete_frames(frames)
        print("Temporary frame files deleted.")

    print_result(response)
