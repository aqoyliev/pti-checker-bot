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
FILE_API_THRESHOLD = 300   # frames above this count are uploaded via File API instead of sent inline


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
  3. Fire extinguisher and warning triangle (presence)
  4. Tires — tread depth and air pressure (truck and trailer)
  5. Side mirrors — 4 total, 2 on the hood (flag only clearly broken / missing / cracked)
  6. Under the hood — engine oil level and visible leaks
  7. Windshield (flag only clear cracks/chips in the driver's view)
  8. Air lines (flag only visible cuts, leaks, or disconnected lines)
  9. Overall frame / chassis (flag only clearly cracked, bent, or damaged frame members)

Leniency rules — BE CONSERVATIVE. The default is PASS. It is FAR worse to falsely flag a good component than to miss a borderline defect. Drivers lose trust in this bot when it invents issues. When in doubt: PASS. Never INVENT a defect you cannot describe precisely (size, shape, location, what makes you certain).

  - **Tires — be extra strict before flagging. Past false positives have happened on completely good tires.**
    - **Tread depth — measurement vs. visible wear are different.** You CANNOT measure tread depth from a video or photo, so NEVER quote a number ("below 4/32", "2/32 left") or invoke "wear bars" — you have no measurement tool, and inventing one is a hallucination. BUT you CAN see severe visible wear with your eyes: if a tire has clearly smooth or bald patches where the tread pattern is missing or visibly faded compared to the rest of the same tire OR the adjacent dual tire, that IS a flaggable defect. Describe what you actually see — location on the tire, smoothness, contrast to neighbouring rubber. Format: "(M:SS) Trailer inner tire bald in center tread (49 CFR 393.75)" — NOT "worn below 4/32".
    - "Cracked sidewall" requires a visible split in the rubber where you can plainly see a gap or separation in the surface. Weathered-looking rubber, normal sidewall lettering/DOT codes, brand markings, mold lines, shadows, dirt, mud, or texture variation are NOT cracks. If you have to squint, it is not a crack.
    - "Cut sidewall" requires a visible incision — a clean line where the rubber is severed. Scuff marks, paint, mud, shadows, stickers, and brand stamps are NOT cuts.
    - "Bulge" requires a clearly protruding section, not normal sidewall curvature.
    - **Puncture / hole / embedded object**: DO flag a clearly visible dark cavity in the tread surface or sidewall, a deep gouge where rubber is missing, or a nail/screw/sharp object embedded in the rubber. A puncture looks like a distinct dark spot or pit in the rubber face itself — NOT a normal tread groove, NOT a stone or pebble lodged between tread blocks, NOT a small bit of dirt. If you can point to a specific location on the tread (e.g., "between the second and third grooves") and describe a cavity in the rubber, flag it. Format: "(M:SS) Puncture in trailer tire tread (49 CFR 393.75)".
    - A tire is FINE unless you can clearly see one of: visibly flat (deformed against the ground), exposed cord/belt fabric, an unmistakable sidewall split or incision, a clear bulge, a puncture/hole/embedded object in the tread or sidewall, or clearly visible bald/smooth patches where the tread pattern is missing or severely faded compared to the rest of the tire or the adjacent dual. Otherwise treat it as PASS and add "Tires" to "checked_clean".
  - Air bags / air suspension bellows ("balloons"): these are NOT in scope. Do not flag them. A bag that looks intact is fine.
  - Lights: only flag if a light is obviously broken (visible shattered lens), missing from its housing, or clearly not illuminating when others around it are. Don't fail on dirt, reflections, or being turned off.
  - Windshield: small stone chips outside the driver's line of sight are NOT a defect; only flag long/spreading cracks or chips in the swept area.
  - If you are unsure whether something is a defect, treat it as PASS and (if relevant) add it to "what_was_not_visible" rather than "issues".

Process:
  - Look across ALL frames before deciding — a defect visible in one frame is still a defect.
  - Severity: NONE = all clear; MINOR = small problem, can drive; MAJOR = needs fixing soon; CRITICAL = unsafe, do not drive.
  - VEHICLE IDENTIFICATION: Read visible unit numbers (painted on cab or trailer body) and license plates for the truck and trailer separately. Return one entry per distinct vehicle in "vehicles". If previous PTI history is provided, check whether prior issues are now fixed.

Output rules — the driver reads this on a phone, so be brutally short:
  - Frames from a video are labeled "Video frame at M:SS". Plain photos are labeled "Photo N".
  - For any defect you spot in a video frame, prefix the issue with that frame's timestamp in parens, e.g. "(1:14)". If the same defect spans several frames, use the timestamp where it's clearest. For defects only seen in a Photo (not a video frame), do NOT add a timestamp prefix.
  - Each issue ≤ 8 words of plain English, then ONE CFR citation in parens.
    Format: "(M:SS) <short defect> (49 CFR <section>)" for video defects, or "<short defect> (49 CFR <section>)" for photo defects.
    Good: "(1:14) Cracked steer rim (49 CFR 393.205(a))", "Trailer plate missing (49 CFR 393.17)".
    Bad:  "Cracked rim on passenger side steer wheel (49 CFR 393.205(a), 396 Appendix G, Item 1.a.1)".
    If no clear CFR applies (e.g. minor cosmetic damage), omit the CFR parens entirely (but keep the timestamp).
  - "checked_clean": list which of the 9 inspection areas you actually saw and verified are fine.
    Use ONLY these component labels (one per inspection area):
      "Brake pads", "Lights", "Fire extinguisher & triangle", "Tires", "Mirrors",
      "Under hood", "Windshield", "Air lines", "Frame".
    Each entry is just the component name — no timestamps, no extra text.
    Example: ["Tires", "Mirrors", "Lights", "Windshield"].
    Omit any component you couldn't see clearly. Don't put a component in both "issues" and "checked_clean".
  - "what_was_not_visible": at most 5 short items, only the most important ones. Don't list every PTI area you didn't see — just the ones a driver could reasonably re-shoot.
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
  "issues": ["short plain-English defect"],
  "checked_clean": ["Tires", "Lights", ...],
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
        issue_text = "; ".join(issues) if issues else "none"
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
