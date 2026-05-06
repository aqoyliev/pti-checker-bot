import sys
import os
import json
import tempfile

import cv2
import PIL.Image
from dotenv import load_dotenv
import google.generativeai as genai

load_dotenv()

SYSTEM_PROMPT = (
    "You are an expert commercial truck Pre-Trip Inspection (PTI) analyst with 20+ years of DOT compliance experience. "
    "You are seeing 7 frames extracted evenly from a PTI walk-around video. Analyze all frames together as one inspection. "
    "Respond ONLY with a raw JSON object, no markdown, no code fences:\n"
    "{\n"
    '  "status": "PASS" or "FAIL",\n'
    '  "severity": "NONE" or "MINOR" or "MAJOR" or "CRITICAL",\n'
    '  "confidence": "LOW" or "MEDIUM" or "HIGH",\n'
    '  "image_quality": "POOR" or "ACCEPTABLE" or "GOOD",\n'
    '  "issues": ["issue 1", "issue 2"],\n'
    '  "what_was_visible": ["truck parts clearly seen across frames"],\n'
    '  "what_was_not_visible": ["required PTI areas not seen in any frame"],\n'
    '  "advice": "short actionable advice for the driver"\n'
    "}"
)


def extract_frames(video_path: str, num_frames: int = 7) -> list[tuple[float, str]]:
    if not os.path.exists(video_path):
        print(f"Error: File not found: {video_path}")
        sys.exit(1)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"Error: OpenCV could not open video: {video_path}")
        sys.exit(1)

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS)

    if total_frames == 0 or fps == 0:
        print("Error: Could not read video properties (0 frames or 0 fps).")
        cap.release()
        sys.exit(1)

    duration = total_frames / fps
    temp_dir = tempfile.mkdtemp(prefix="pti_frames_")
    saved: list[tuple[float, str]] = []

    for i in range(num_frames):
        timestamp = duration * (2 * i + 1) / (2 * num_frames)
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(timestamp * fps))
        ret, frame = cap.read()
        if not ret:
            print(f"Warning: Could not read frame at {timestamp:.2f}s, skipping.")
            continue
        path = os.path.join(temp_dir, f"frame_{i:02d}.jpg")
        cv2.imwrite(path, frame)
        saved.append((timestamp, path))
        print(f"  Frame {i + 1}: {timestamp:.2f}s")

    cap.release()
    return saved


def call_gemini(frames: list[tuple[float, str]]) -> genai.types.GenerateContentResponse:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "your-gemini-key-here":
        print("Error: GEMINI_API_KEY is not set in .env")
        sys.exit(1)

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        model_name="gemini-2.0-flash",
        system_instruction=SYSTEM_PROMPT,
    )

    parts = []
    for i, (_, path) in enumerate(frames):
        parts.append(PIL.Image.open(path))
        parts.append(f"Frame {i + 1} of 7")
    parts.append("Analyze all 7 frames above as a single PTI inspection and return the JSON result.")

    try:
        response = model.generate_content(parts)
    except Exception as e:
        print(f"API Error: {e}")
        sys.exit(1)

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


def print_result(response: genai.types.GenerateContentResponse) -> None:
    raw_text = response.text

    print("\n--- RESULT ---")
    try:
        parsed = json.loads(raw_text)
        print(json.dumps(parsed, indent=2))
    except json.JSONDecodeError:
        print("Warning: Response is not valid JSON. Raw output:")
        print(raw_text)

    usage = response.usage_metadata
    input_tokens = usage.prompt_token_count
    output_tokens = usage.candidates_token_count

    print("\n--- USAGE ---")
    print(f"  Input tokens:  {input_tokens}")
    print(f"  Output tokens: {output_tokens}")
    print(f"  Cost: FREE (Gemini free tier)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python test_pti.py <path_to_video>")
        sys.exit(1)

    video_path = sys.argv[1]

    print(f"Extracting 7 frames from: {video_path}")
    frames = extract_frames(video_path)

    if not frames:
        print("Error: No frames could be extracted from the video.")
        sys.exit(1)

    print(f"\nSending {len(frames)} frames to Gemini 2.0 Flash...")
    response = call_gemini(frames)

    delete_frames(frames)
    print("Temporary frame files deleted.")

    print_result(response)
