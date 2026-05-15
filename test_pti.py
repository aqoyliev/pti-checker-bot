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

PTI_FRAME_INTERVAL = float(os.getenv("PTI_FRAME_INTERVAL", "1"))

SYSTEM_PROMPT = """You are an experienced commercial-truck inspector. \
Analyze the supplied frames/photos from a pre-trip inspection (PTI) of a semi-truck and/or trailer as one combined inspection.

Inspection process:
1. Scan each frame for visible defects: flat or worn tires, missing lug nuts, broken/non-working lights, fluid leaks, cracked frame, damaged mirrors, broken reflectors, air-line problems, cracked windshield, etc.
2. Look across ALL frames before deciding — a defect visible in one frame is still a defect.
3. Severity: NONE = all clear; MINOR = small problem, can drive; MAJOR = needs fixing soon; CRITICAL = unsafe, do not drive.
4. VEHICLE IDENTIFICATION: Read visible unit numbers (painted on cab or trailer body) and license plates for the truck and trailer separately. Return one entry per distinct vehicle in "vehicles". If previous PTI history is provided, check whether prior issues are now fixed.

Output rules — the driver reads this on a phone, so be brutally short:
  - Frames from a video are labeled "Video frame at M:SS". Plain photos are labeled "Photo N".
  - For any defect you spot in a video frame, prefix the issue with that frame's timestamp in parens, e.g. "(1:14)". If the same defect spans several frames, use the timestamp where it's clearest. For defects only seen in a Photo (not a video frame), do NOT add a timestamp prefix.
  - Each issue ≤ 8 words of plain English, then ONE CFR citation in parens.
    Format: "(M:SS) <short defect> (49 CFR <section>)" for video defects, or "<short defect> (49 CFR <section>)" for photo defects.
    Good: "(1:14) Cracked steer rim (49 CFR 393.205(a))", "Trailer plate missing (49 CFR 393.17)".
    Bad:  "Cracked rim on passenger side steer wheel (49 CFR 393.205(a), 396 Appendix G, Item 1.a.1)".
    If no clear CFR applies (e.g. minor cosmetic damage), omit the CFR parens entirely (but keep the timestamp).
  - "checked_clean": list which broad component groups you actually saw and verified are fine, each with the timestamps where you saw them clearly.
    Use ONLY these component labels: "Tires", "Lights", "Mirrors", "Body/Frame", "Mud flaps", "Leaks", "Windshield", "Reflectors".
    Format each entry as: "<Component> — <moments>" where moments is a comma-separated list of M:SS timestamps and/or M:SS-M:SS ranges.
    Example: "Mirrors — 0:11-0:13, 2:12-2:15, 2:20".
    Use a range when the component is in view across consecutive frames; use a single timestamp when it's a brief glimpse. List 1–4 moments per component, the clearest ones.
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
        cv2.imwrite(path, frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
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
        model="gemini-2.5-pro",
        config=genai_types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0,
            response_mime_type="application/json",
        ),
        contents=parts,
    )
    return response


def call_gemini_photos(images: list[tuple], history: list[dict] | None = None):
    """images: list of (file_path, mime_type) or (file_path, mime_type, label).
    Label is shown to Gemini after each image — e.g. "Video frame at 1:14" or "Photo 2".
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "your-gemini-key-here":
        raise ValueError("GEMINI_API_KEY is not set in .env")

    client = genai.Client(api_key=api_key)

    n = len(images)
    parts = []
    for i, item in enumerate(images):
        if len(item) == 3:
            path, mime_type, label = item
        else:
            path, mime_type = item
            label = f"Photo {i + 1} of {n}"
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
