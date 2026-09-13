"""What the two report PDFs are allowed to claim.

``build`` is pure -- rows in, printed numbers out -- so the arithmetic behind a
published figure is testable without a database or a browser, and that is the
whole reason it is a separate function from ``fetch``.
"""

import importlib.util
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_path = Path(__file__).resolve().parent.parent / "scripts" / "fleet_report.py"
_spec = importlib.util.spec_from_file_location("fleet_report", _path)
fleet_report = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = fleet_report
_spec.loader.exec_module(fleet_report)

TZ = ZoneInfo("America/New_York")
SINCE, UNTIL = date(2026, 8, 17), date(2026, 8, 24)
CLEAN = {"missing_areas": [], "fire_extinguisher_shown": True,
         "what_was_not_visible": []}


def group(gid, unit, *, active=True, setup=True):
    return {"group_id": gid, "unit_number": unit, "title": f"UNIT {unit}",
            "setup_complete": setup, "is_active": active}


def submission(gid, uid, day=18, result=None):
    return {"id": gid * 100 + uid, "group_id": gid, "user_id": uid,
            "driver_name": "TG NAME", "submitted_at": datetime(2026, 8, day, 14, 0),
            "passed": True, "unit_number": "?",
            "result_json": CLEAN if result is None else result}


def build(groups, drivers, window, alltime=None):
    return fleet_report.build(
        {"groups": groups, "drivers": drivers, "window": window,
         "alltime": alltime or {}}, TZ, SINCE, UNTIL)


# ------------------------------------------------- retired trucks are excluded

def test_a_retired_groups_submission_is_not_counted():
    """Every other list in the report skips inactive groups. If the window
    count does not, the coverage headline divides submissions from the whole
    history by the units still running -- and can print over 100%."""
    agg = build(
        groups=[group(-1, "1101"), group(-2, "1102", active=False)],
        drivers=[{"group_id": -1, "user_id": 11, "name": "A"},
                 {"group_id": -2, "user_id": 22, "name": "B"}],
        window=[submission(-1, 11), submission(-2, 22), submission(-2, 22, day=19)],
    )
    t = agg["totals"]
    assert t["inspections"] == 1
    assert t["units_sent"] == 1
    assert t["units_active"] == 1
    assert t["drivers_sent"] == 1
    assert "B" not in [r["name"] for r in agg["driver_rows"]]


def test_the_roster_denominator_counts_active_groups_only():
    agg = build(
        groups=[group(-1, "1101"), group(-2, "1102", active=False)],
        drivers=[{"group_id": -1, "user_id": 11, "name": "A"},
                 {"group_id": -1, "user_id": 12, "name": "B"},
                 {"group_id": -2, "user_id": 22, "name": "RETIRED"}],
        window=[],
    )
    assert agg["totals"]["drivers_total"] == 2


def test_coverage_can_never_exceed_the_active_fleet():
    agg = build(
        groups=[group(-i, str(1100 + i), active=i <= 2) for i in range(1, 6)],
        drivers=[{"group_id": -i, "user_id": i, "name": f"D{i}"}
                 for i in range(1, 6)],
        window=[submission(-i, i) for i in range(1, 6)],
    )
    t = agg["totals"]
    assert t["units_sent"] <= t["units_active"]
    assert t["drivers_sent"] <= t["drivers_total"]


def test_a_submission_from_an_unknown_group_is_kept():
    """No ``groups`` row means unknown, which is not the same as retired --
    dropping it would quietly lose a real inspection."""
    agg = build(groups=[group(-1, "1101")], drivers=[],
                window=[submission(-1, 11), submission(-99, 99)])
    assert agg["totals"]["inspections"] == 2


# ----------------------------------------------------- the silent are the point

def test_a_driver_who_sent_nothing_still_appears():
    agg = build(
        groups=[group(-1, "1101")],
        drivers=[{"group_id": -1, "user_id": 11, "name": "SENT"},
                 {"group_id": -1, "user_id": 12, "name": "SILENT"}],
        window=[submission(-1, 11)],
    )
    silent = next(r for r in agg["driver_rows"] if r["name"] == "SILENT")
    assert silent["submissions"] == 0
    assert silent["items"] == []


def test_a_unit_that_never_sent_one_is_counted_apart_from_the_merely_quiet():
    agg = build(
        groups=[group(-1, "1101"), group(-2, "1102")],
        drivers=[],
        window=[],
        alltime={-1: {"group_id": -1, "n": 40,
                      "last_at": datetime(2026, 7, 1, 12, 0)}},
    )
    t = agg["totals"]
    assert t["silent"] == 2
    assert t["never_ever"] == 1


def test_every_day_of_the_window_is_plotted_including_the_empty_ones():
    agg = build(groups=[group(-1, "1101")], drivers=[],
                window=[submission(-1, 11, day=18)])
    assert [d["day"] for d in agg["daily"]] == [
        date(2026, 8, d) for d in range(17, 24)]
    assert sum(d["n"] for d in agg["daily"]) == 1


# ------------------------------------------------- the document stands alone

@pytest.fixture
def meta():
    return {"fleet": "acme", "scope": "acme-pti / production",
            "since": SINCE, "until": UNTIL, "pulled": date(2026, 8, 25),
            "tz": "America/New_York", "title": "acme stats",
            "title_d": "acme driver report"}


@pytest.mark.parametrize("render", ["stats_html", "driver_html"])
def test_the_report_html_pulls_nothing_off_the_network(render, meta):
    """Rendering is a headless browser on a container with no fonts and no
    reason to have outbound access. Everything the page needs -- the typeface
    included -- has to already be in the page."""
    agg = build(groups=[group(-1, "1101")],
                drivers=[{"group_id": -1, "user_id": 11, "name": "A"}],
                window=[submission(-1, 11)])
    page = getattr(fleet_report, render)(agg, meta)
    assert "data:font/woff2;base64," in page
    assert "http://" not in page
    assert "https://" not in page
