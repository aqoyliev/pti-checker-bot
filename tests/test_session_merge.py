"""A walkaround filmed in three clips is one inspection, not three partials.

Two things are pinned here. `merge_session` is the arithmetic -- the union of
what the clips showed -- and it must be a no-op for a session of one, because
nearly every driver sends a single video and their published numbers may not
move. `sessions_from` is the grouping: a rolling gap, so a split walkaround
holds together while a genuine second PTI later in the day stays a second one.
"""

import importlib.util
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from utils import report_scoring
from utils.report_scoring import merge_session, score_inspection

_path = Path(__file__).resolve().parent.parent / "scripts" / "fleet_report.py"
_spec = importlib.util.spec_from_file_location("fleet_report", _path)
fleet_report = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = fleet_report
_spec.loader.exec_module(fleet_report)

ALL = list(report_scoring.REQUIRED_AREAS)


def clip(*, filmed, fe=True, not_visible=()):
    """A clip that filmed exactly ``filmed`` of the required areas."""
    return score_inspection({
        "missing_areas": [a for a in ALL if a not in filmed],
        "checked_clean": list(filmed),
        "fire_extinguisher_shown": fe,
        "what_was_not_visible": list(not_visible),
    })


# ----------------------------------------------------- the union of the clips

def test_a_session_of_one_is_that_submission_untouched():
    one = clip(filmed=ALL[:5])
    assert merge_session([one]) is one


def test_three_thirds_of_a_walkaround_add_up_to_the_walkaround():
    clips = [clip(filmed=ALL[0:3]), clip(filmed=ALL[3:6]), clip(filmed=ALL[6:8])]
    assert [c.klass for c in clips] == ["Partial", "Partial", "Not a PTI"]
    assert not any(c.is_real for c in clips)

    merged = merge_session(clips)
    assert merged.missing == []
    assert merged.filmed == 8
    assert merged.klass == "Complete"
    assert merged.is_real is True
    assert merged.score == 100


def test_an_area_no_clip_filmed_is_still_missing():
    merged = merge_session([clip(filmed=ALL[0:4]), clip(filmed=ALL[2:7])])
    assert merged.missing == [ALL[7]]
    assert merged.klass == "Real"


def test_missing_areas_read_in_the_canonical_order_whatever_the_clip_order():
    clips = [clip(filmed=ALL[2:]), clip(filmed=ALL[2:])]
    assert merge_session(clips).missing == [ALL[0], ALL[1]]
    assert merge_session(clips[::-1]).missing == [ALL[0], ALL[1]]


def test_the_extinguisher_counts_if_any_clip_showed_it():
    merged = merge_session([clip(filmed=ALL[:4], fe=False),
                            clip(filmed=ALL[4:], fe=True)])
    assert merged.fire_extinguisher is True
    assert merged.score == 100


def test_the_extinguisher_no_clip_showed_still_costs_its_five_points():
    merged = merge_session([clip(filmed=ALL[:4], fe=False),
                            clip(filmed=ALL[4:], fe=False)])
    assert merged.fire_extinguisher is False
    assert merged.score == 95


def test_a_sub_item_only_one_clip_flagged_is_not_charged_for():
    """A clip that never pointed at the trailer is no evidence about its tape."""
    merged = merge_session([
        clip(filmed=ALL[:4], not_visible=["DOT tape", "left mirror glass"]),
        clip(filmed=ALL[4:], not_visible=["left mirror glass"]),
    ])
    assert merged.not_visible == ["left mirror glass"]
    assert merged.score == 98  # 85 + 5 + (10 - 2)


def test_an_empty_session_is_a_programming_error_not_a_zero_score():
    with pytest.raises(ValueError):
        merge_session([])


def test_the_merged_score_uses_the_published_formula():
    merged = merge_session([clip(filmed=ALL[0:3]), clip(filmed=ALL[3:7])])
    assert merged.filmed == 7 and merged.required == 8
    assert merged.score == round(85 * 7 / 8) + 5 + 10


# --------------------------------------------------------------- the grouping

def sub_row(minutes, *, gid=-1, uid=11, score=None, passed=False):
    at = datetime(2026, 8, 18, 14, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)
    return {"at": at, "day": at.date(), "group_id": gid, "user_id": uid,
            "unit": "1101", "name": "A", "passed": passed,
            "score": score if score is not None else clip(filmed=ALL)}


def test_clips_posted_back_to_back_are_one_session():
    out = fleet_report.sessions_from([sub_row(0), sub_row(4), sub_row(9)])
    assert len(out) == 1
    assert out[0]["clips"] == 3


def test_a_second_pti_hours_later_is_a_second_pti():
    out = fleet_report.sessions_from([sub_row(0), sub_row(8 * 60)])
    assert [s["clips"] for s in out] == [1, 1]


def test_the_gap_rolls_so_a_long_walkaround_holds_together():
    """Each clip is within the gap of the one before it, not of the first."""
    out = fleet_report.sessions_from([sub_row(0), sub_row(20), sub_row(45)])
    assert len(out) == 1 and out[0]["clips"] == 3


def test_a_clip_just_past_the_gap_starts_a_new_session():
    gap = report_scoring.SESSION_GAP_MINUTES
    assert len(fleet_report.sessions_from([sub_row(0), sub_row(gap)])) == 1
    assert len(fleet_report.sessions_from([sub_row(0), sub_row(gap + 1)])) == 2


def test_two_drivers_filming_at_once_are_two_walkarounds():
    out = fleet_report.sessions_from([sub_row(0, uid=11), sub_row(1, uid=12)])
    assert len(out) == 2


def test_the_same_driver_in_two_groups_is_two_walkarounds():
    out = fleet_report.sessions_from([sub_row(0, gid=-1), sub_row(1, gid=-2)])
    assert len(out) == 2


def test_a_session_passes_when_its_clips_filmed_everything_between_them():
    out = fleet_report.sessions_from([
        sub_row(0, score=clip(filmed=ALL[0:4]), passed=False),
        sub_row(5, score=clip(filmed=ALL[4:8]), passed=False),
    ])
    assert len(out) == 1
    assert out[0]["passed"] is True
    assert out[0]["score"].missing == []


def test_a_session_still_short_of_an_area_does_not_pass():
    out = fleet_report.sessions_from([
        sub_row(0, score=clip(filmed=ALL[0:4]), passed=False),
        sub_row(5, score=clip(filmed=ALL[4:7]), passed=False),
    ])
    assert out[0]["passed"] is False


def test_sessions_come_back_in_time_order():
    out = fleet_report.sessions_from([
        sub_row(600, uid=12), sub_row(0, uid=11), sub_row(300, uid=13)])
    assert [s["at"].hour for s in out] == [14, 19, 0]


# -------------------------------------------------- what the report then says

TZ = fleet_report.ZoneInfo("America/New_York")


def test_a_split_walkaround_counts_once_in_the_report():
    """The whole point: three clips, one real PTI -- not three partials."""
    def row(i, minute, filmed):
        return {"id": i, "group_id": -1, "user_id": 11, "driver_name": "A",
                "submitted_at": datetime(2026, 8, 18, 14, minute),
                "passed": False, "unit_number": "1101",
                "result_json": {
                    "missing_areas": [a for a in ALL if a not in filmed],
                    "checked_clean": list(filmed),
                    "fire_extinguisher_shown": True,
                    "what_was_not_visible": []}}

    agg = fleet_report.build(
        {"groups": [{"group_id": -1, "unit_number": "1101", "title": "UNIT 1101",
                     "setup_complete": True, "is_active": True}],
         "drivers": [{"group_id": -1, "user_id": 11, "name": "A"}],
         "window": [row(1, 0, ALL[0:3]), row(2, 5, ALL[3:6]), row(3, 11, ALL[6:8])],
         "alltime": {}},
        TZ, date(2026, 8, 17), date(2026, 8, 24))

    t = agg["totals"]
    assert t["inspections"] == 1
    assert t["real"] == 1
    assert t["passed"] == 1
    assert t["avg"] == 100
    row_a = next(r for r in agg["driver_rows"] if r["name"] == "A")
    assert row_a["inspections"] == 1 and row_a["clips"] == 3 and row_a["real"] == 1
    assert sum(d["n"] for d in agg["daily"]) == 1


def test_two_separate_ptis_a_day_apart_stay_two_rows():
    """The merge must not quietly collapse a driver's second walkaround."""
    def row(i, day):
        return {"id": i, "group_id": -1, "user_id": 11, "driver_name": "A",
                "submitted_at": datetime(2026, 8, day, 14, 0),
                "passed": True, "unit_number": "1101",
                "result_json": {"missing_areas": [], "checked_clean": ALL,
                                "fire_extinguisher_shown": True,
                                "what_was_not_visible": []}}

    agg = fleet_report.build(
        {"groups": [{"group_id": -1, "unit_number": "1101", "title": "UNIT 1101",
                     "setup_complete": True, "is_active": True}],
         "drivers": [{"group_id": -1, "user_id": 11, "name": "A"}],
         "window": [row(1, 18), row(2, 19)], "alltime": {}},
        TZ, date(2026, 8, 17), date(2026, 8, 24))
    assert agg["totals"]["inspections"] == 2
    assert next(r for r in agg["driver_rows"] if r["name"] == "A")["real"] == 2
