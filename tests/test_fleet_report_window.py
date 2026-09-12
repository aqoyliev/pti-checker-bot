"""The report window, which is where an off-by-one silently prints a wrong week."""

import importlib.util
import sys
from argparse import Namespace
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_path = Path(__file__).resolve().parent.parent / "scripts" / "fleet_report.py"
_spec = importlib.util.spec_from_file_location("fleet_report", _path)
fleet_report = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = fleet_report
_spec.loader.exec_module(fleet_report)

TZ = ZoneInfo("America/New_York")


def args(**kw):
    base = dict(since=None, until=None, last_week=False, days=7)
    base.update(kw)
    return Namespace(**base)


@pytest.fixture
def today(monkeypatch):
    """Freeze "now" at Wednesday 26 Aug 2026."""
    class FrozenDT(fleet_report.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 26, 10, 30, tzinfo=tz)

    monkeypatch.setattr(fleet_report, "datetime", FrozenDT)


def test_last_week_is_the_most_recently_completed_monday_to_sunday(today):
    since, until = fleet_report.resolve_window(args(last_week=True), TZ)
    # 26 Aug 2026 is a Wednesday, so last week ran Mon 17 - Sun 23 Aug.
    assert since == date(2026, 8, 17)
    assert until == date(2026, 8, 24)      # exclusive
    assert since.weekday() == 0
    assert (until - since).days == 7


def test_last_week_on_a_monday_does_not_report_the_week_in_progress(monkeypatch):
    class OnMonday(fleet_report.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 24, 6, 0, tzinfo=tz)

    monkeypatch.setattr(fleet_report, "datetime", OnMonday)
    since, until = fleet_report.resolve_window(args(last_week=True), TZ)
    assert (since, until) == (date(2026, 8, 17), date(2026, 8, 24))


def test_days_window_includes_today_and_spans_exactly_n_days(today):
    since, until = fleet_report.resolve_window(args(days=7), TZ)
    assert (until - since).days == 7
    assert until == date(2026, 8, 27)      # exclusive: today is counted
    assert since == date(2026, 8, 20)


def test_explicit_since_until_is_taken_verbatim(today):
    since, until = fleet_report.resolve_window(
        args(since="2026-06-24", until="2026-08-07"), TZ)
    assert (since, until) == (date(2026, 6, 24), date(2026, 8, 7))


def test_since_without_until_runs_through_today(today):
    since, until = fleet_report.resolve_window(args(since="2026-08-01"), TZ)
    assert (since, until) == (date(2026, 8, 1), date(2026, 8, 27))


def test_display_name_does_not_repeat_pti():
    assert fleet_report.display_name("jrd-pti") == "jrd"
    assert fleet_report.display_name("gurman-pti") == "gurman"
    assert fleet_report.display_name("jrd") == "jrd"
