"""Regression test: the frame files must outlive the tire-pass task.

process_mixed_media used to run the broad pass and the tire pass in one
asyncio.gather(); when the broad pass raised (e.g. Gemini 503 after all keys),
gather propagated immediately without waiting for the tire task, and the
finally block deleted the extracted frames while the orphaned tire pass was
still uploading them (FileNotFoundError mid-upload). The fix awaits the tire
task in the finally block before deleting frames.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from data import config
from utils import pti_processor as pp


def test_frames_outlive_tire_pass_when_broad_pass_fails(monkeypatch, tmp_path):
    events = []

    async def slow_tire_pass(all_images):
        await asyncio.sleep(0.05)
        events.append("tire_done")
        return None

    async def failing_broad_pass(fn, *args, **kwargs):
        raise RuntimeError("broad pass exploded")

    frame = tmp_path / "frame_00001.jpg"
    frame.write_bytes(b"jpg")

    monkeypatch.setattr(config, "PTI_TIRE_PASS", True)
    monkeypatch.setattr(config, "PTI_SPLIT_FRAMES", False)
    monkeypatch.setattr(config, "LOCAL_SERVER_URL", "")
    monkeypatch.setattr(pp, "_analysis_semaphore", None)
    monkeypatch.setattr(pp, "get_api_keys", lambda: ["k1"])
    monkeypatch.setattr(pp, "extract_frames", lambda path: [(0.0, str(frame))])
    monkeypatch.setattr(pp, "delete_frames", lambda frames: events.append("frames_deleted"))
    monkeypatch.setattr(pp, "_call_gemini_with_retry", failing_broad_pass)
    monkeypatch.setattr(pp, "_run_tire_pass", slow_tire_pass)
    monkeypatch.setattr(pp, "bot", SimpleNamespace(download_file=AsyncMock()))

    status_msg = SimpleNamespace(edit_text=AsyncMock())
    reply_to = SimpleNamespace(
        reply=AsyncMock(return_value=status_msg),
        answer=AsyncMock(return_value=status_msg),
    )
    items = [{"kind": "video", "obj": SimpleNamespace(get_file=AsyncMock(return_value=SimpleNamespace(file_path="remote/path.mp4")))}]

    text, data, status = asyncio.run(pp.process_mixed_media(items, reply_to))

    assert text is None and data is None
    assert events == ["tire_done", "frames_deleted"]
