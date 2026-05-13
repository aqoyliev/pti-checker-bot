from __future__ import annotations

import sys
import os
import json
import logging
import tempfile

import cv2
from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types

load_dotenv()

PTI_FRAME_INTERVAL = float(os.getenv("PTI_FRAME_INTERVAL", "2"))

SYSTEM_PROMPT = """You are a professional US Department of Transportation (DOT) Vehicle Inspector with 20+ years of field experience. \
You are analyzing media (video frames or photos) from a PTI walk-around inspection of a commercial semi-truck or trailer. \
Treat all provided frames/photos together as a single unified inspection.

Your inspection protocol:
1. IDENTIFY components visible across the media (e.g., steer tires, drive tires, 5th wheel, glad hands, brake chambers, lights, reflectors, fuel tank, exhaust, frame, cab, mirrors).
2. SCAN for defects: cuts/bulges/low tread on tires, air leaks, missing lug nuts, cracked leaf springs, non-functional lights, damaged reflective tape, fluid leaks, bent/cracked frame members.
3. Use DOT terminology (e.g., ABC check — Airlines, Bill of Lading, Connections; CMS — Cracked, Missing, Stuck).
4. If a photo is too dark or blurry to assess, note it under image_quality and list that area under what_was_not_visible.
5. Severity definitions: NONE = all clear; MINOR = monitor but drivable; MAJOR = repair soon, drivable short-term; CRITICAL = Out of Service (OOS), do not move vehicle.
6. VEHICLE IDENTIFICATION: Read any visible unit numbers (painted on cab door or body) and license plates. If previous PTI history is provided, check whether previously flagged issues have been resolved in this submission.

Respond ONLY with a raw JSON object — no markdown, no code fences:
{
  "status": "PASS" or "FAIL",
  "severity": "NONE" or "MINOR" or "MAJOR" or "CRITICAL",
  "confidence": "LOW" or "MEDIUM" or "HIGH",
  "image_quality": "POOR" or "ACCEPTABLE" or "GOOD",
  "issues": ["specific defect with DOT regulation reference if applicable"],
  "what_was_visible": ["truck parts clearly seen across frames"],
  "what_was_not_visible": ["required PTI areas not seen or too dark/blurry to assess"],
  "advice": "concise actionable advice for the driver, referencing 49 CFR if OOS",
  "vehicle": {
    "type": "truck" or "trailer" or null,
    "unit_number": "visible unit number or null",
    "plate": "visible license plate or null"
  },
  "previously_flagged_resolved": ["issue from history confirmed fixed"] or []
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


def extract_frames(video_path: str, interval_seconds: float = PTI_FRAME_INTERVAL) -> list[tuple[float, str]]:
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video file not found: {video_path}")

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"OpenCV could not open video: {video_path}")

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS)

    if total_frames == 0 or fps == 0:
        cap.release()
        raise RuntimeError("Could not read video properties (0 frames or 0 fps).")

    duration = total_frames / fps
    temp_dir = tempfile.mkdtemp(prefix="pti_frames_")
    saved: list[tuple[float, str]] = []

    timestamp = 0.0
    i = 0
    while timestamp < duration:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(timestamp * fps))
        ret, frame = cap.read()
        if not ret:
            logging.warning(f"Could not read frame at {timestamp:.2f}s, skipping.")
            timestamp += interval_seconds
            i += 1
            continue
        h, w = frame.shape[:2]
        if max(h, w) > 1280:
            scale = 1280 / max(h, w)
            frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        path = os.path.join(temp_dir, f"frame_{i:04d}.jpg")
        cv2.imwrite(path, frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        saved.append((timestamp, path))
        timestamp += interval_seconds
        i += 1

    cap.release()
    logging.info(f"Extracted {len(saved)} frames from {os.path.basename(video_path)} ({duration:.1f}s @ {interval_seconds}s interval)")
    return saved


def call_gemini(frames: list[tuple[float, str]], history: list[dict] | None = None):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "your-gemini-key-here":
        raise ValueError("GEMINI_API_KEY is not set in .env")

    client = genai.Client(api_key=api_key)

    n = len(frames)
    parts = []
    for i, (_, path) in enumerate(frames):
        with open(path, "rb") as f:
            parts.append(genai_types.Part.from_bytes(data=f.read(), mime_type="image/jpeg"))
        parts.append(f"Frame {i + 1} of {n}")

    history_text = _build_history_text(history or [])
    if history_text:
        parts.append(history_text)

    parts.append(f"Analyze all {n} frames above as a single PTI inspection and return the JSON result.")

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        config=genai_types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, temperature=0.2),
        contents=parts,
    )
    return response


def call_gemini_photos(images: list[tuple[str, str]], history: list[dict] | None = None):
    """images: list of (file_path, mime_type)"""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "your-gemini-key-here":
        raise ValueError("GEMINI_API_KEY is not set in .env")

    client = genai.Client(api_key=api_key)

    n = len(images)
    parts = []
    for i, (path, mime_type) in enumerate(images):
        with open(path, "rb") as f:
            parts.append(genai_types.Part.from_bytes(data=f.read(), mime_type=mime_type))
        parts.append(f"Photo {i + 1} of {n}")

    history_text = _build_history_text(history or [])
    if history_text:
        parts.append(history_text)

    parts.append(f"Analyze all {n} photo(s) above as a single PTI inspection and return the JSON result.")

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        config=genai_types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, temperature=0.2),
        contents=parts,
    )
    return response


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
    return json.loads(response.text)


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
