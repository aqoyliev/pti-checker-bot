"""Unit tests for split-frame analysis: even chunking and per-chunk result merging.

Pure: no network. These cover the two pieces that must stay correct when one video's
frames are spread across multiple API keys — the split, and the merge that rebuilds a
single verdict so a fully-filmed truck doesn't FAIL just because no single chunk saw
all 8 areas.
"""
from utils import pti_processor as pp


# ---------- _split_evenly ----------

def test_split_evenly_exact():
    chunks = pp._split_evenly(list(range(210)), 3)
    assert [len(c) for c in chunks] == [70, 70, 70]
    # contiguous and lossless
    assert chunks[0][0] == 0 and chunks[2][-1] == 209
    assert sum((c for c in chunks), []) == list(range(210))


def test_split_evenly_remainder_goes_to_front():
    chunks = pp._split_evenly(list(range(211)), 3)
    assert [len(c) for c in chunks] == [71, 70, 70]


def test_split_evenly_more_keys_than_frames():
    chunks = pp._split_evenly([1, 2], 3)
    # never more chunks than items, never empty chunks
    assert [len(c) for c in chunks] == [1, 1]


# ---------- merge_frame_passes ----------

def test_merge_unions_clean_and_concats_issues():
    passes = [
        {"checked_clean": ["Lights", "Windshield"], "issues": [{"text": "a", "evidence": "x" * 25}]},
        {"checked_clean": ["Tires", "Lights"], "issues": [{"text": "b", "evidence": "y" * 25}]},
    ]
    merged = pp.merge_frame_passes(passes)
    assert set(c.lower() for c in merged["checked_clean"]) == {"lights", "windshield", "tires"}
    assert len(merged["issues"]) == 2


def test_merge_area_filmed_in_any_chunk_is_not_missing():
    # Chunk 1 only saw the front and calls Tires missing; chunk 2 actually filmed the
    # tires clean. The merged result must NOT mark Tires missing.
    passes = [
        {"checked_clean": ["Lights"], "missing_areas": ["Tires", "Frame"]},
        {"checked_clean": ["Tires"], "missing_areas": ["Lights", "Frame"]},
    ]
    merged = pp.merge_frame_passes(passes)
    missing = {m.lower() for m in merged["missing_areas"]}
    assert "tires" not in missing   # filmed in chunk 2
    assert "lights" not in missing  # filmed in chunk 1
    assert "frame" in missing       # no chunk saw the frame


def test_merge_fire_extinguisher_true_if_any_chunk_saw_it():
    passes = [
        {"fire_extinguisher_shown": False},
        {"fire_extinguisher_shown": True},
    ]
    assert pp.merge_frame_passes(passes)["fire_extinguisher_shown"] is True


def test_merged_result_passes_completeness_when_all_areas_covered_across_chunks():
    # Three chunks that together cover all 8 required areas (none individually does)
    # must yield a PASS after finalize_result, not a FAIL.
    a = list(pp.REQUIRED_AREAS)
    passes = [
        {"checked_clean": a[0:3], "issues": [], "missing_areas": a[3:]},
        {"checked_clean": a[3:6], "issues": [], "missing_areas": a[0:3] + a[6:]},
        {"checked_clean": a[6:8], "issues": [], "missing_areas": a[0:6]},
    ]
    merged = pp.merge_frame_passes(passes)
    incomplete = pp.apply_completeness_verdict(merged)
    assert incomplete is False
    assert merged["status"] == "PASS"
