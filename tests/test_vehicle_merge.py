"""The split-frame merge must carry the vehicle readings through.

`merge_frame_passes` rebuilds the result from a fixed list of keys, and for as
long as "vehicles" was missing from that list every inspection in production
threw its truck and trailer numbers away -- splitting is on by default wherever
there is more than one API key, which is everywhere. Nothing failed; the feature
simply never ran. These pin the merge itself and the fact that the key survives.

Pure: no network, no database.
"""
from utils import pti_processor as pp


def _pass(*vehicles, **extra):
    return {"checked_clean": [], "issues": [], "missing_areas": [],
            "vehicles": list(vehicles), **extra}


# ---------- the key survives the rebuild ----------

def test_merge_frame_passes_keeps_the_vehicles_key():
    merged = pp.merge_frame_passes([
        _pass({"type": "truck", "unit_number": "2570", "plate": "AB12345"}),
        _pass({"type": "trailer", "unit_number": "53821", "plate": None}),
    ])
    assert merged["vehicles"] == [
        {"type": "truck", "unit_number": "2570", "plate": "AB12345"},
        {"type": "trailer", "unit_number": "53821", "plate": None},
    ]


def test_merge_frame_passes_reports_no_vehicles_as_an_empty_list():
    merged = pp.merge_frame_passes([_pass(), _pass()])
    assert merged["vehicles"] == []


# ---------- the vote ----------

def test_two_agreeing_chunks_outrank_one_misread():
    # Frames are strided, so several chunks see the same stencilled number. A
    # single bad read must not win just by landing in an earlier chunk.
    vehicles = pp.merge_vehicles([
        _pass({"type": "truck", "unit_number": "Z570", "plate": None}),
        _pass({"type": "truck", "unit_number": "2570", "plate": None}),
        _pass({"type": "truck", "unit_number": "2570", "plate": None}),
    ])
    assert vehicles == [{"type": "truck", "unit_number": "2570", "plate": None}]


def test_a_tie_goes_to_the_earlier_chunk():
    vehicles = pp.merge_vehicles([
        _pass({"type": "truck", "unit_number": "2570", "plate": None}),
        _pass({"type": "truck", "unit_number": "Z570", "plate": None}),
    ])
    assert vehicles[0]["unit_number"] == "2570"


def test_unit_and_plate_are_voted_on_separately():
    # The chunk that read the plate never got a legible unit number, and vice
    # versa. Both readings still count.
    vehicles = pp.merge_vehicles([
        _pass({"type": "truck", "unit_number": "2570", "plate": None}),
        _pass({"type": "truck", "unit_number": None, "plate": "AB12345"}),
    ])
    assert vehicles == [{"type": "truck", "unit_number": "2570", "plate": "AB12345"}]


def test_placeholder_strings_are_not_readings():
    # The schema says 'visible unit number or null'; the model sometimes writes
    # the word instead of emitting JSON null.
    vehicles = pp.merge_vehicles([
        _pass({"type": "truck", "unit_number": "null", "plate": "unknown"}),
        _pass({"type": "truck", "unit_number": "null", "plate": "n/a"}),
        _pass({"type": "truck", "unit_number": "2570", "plate": None}),
    ])
    assert vehicles == [{"type": "truck", "unit_number": "2570", "plate": None}]


def test_a_vehicle_nobody_read_anything_about_is_dropped():
    vehicles = pp.merge_vehicles([
        _pass({"type": "trailer", "unit_number": None, "plate": None}),
    ])
    assert vehicles == []


def test_unknown_vehicle_types_are_ignored():
    vehicles = pp.merge_vehicles([
        _pass({"type": "forklift", "unit_number": "99", "plate": None},
              {"type": "truck", "unit_number": "2570", "plate": None}),
    ])
    assert [v["type"] for v in vehicles] == ["truck"]


def test_junk_entries_do_not_crash_the_merge():
    assert pp.merge_vehicles([{"vehicles": ["truck 2570", None, 7]}]) == []
    assert pp.merge_vehicles([{}, {"vehicles": None}]) == []
