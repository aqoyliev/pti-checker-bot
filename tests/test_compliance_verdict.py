"""compliance_verdict is the pure core of check_driver_compliance — these pin
its behavior so the batch (get_weekly_pti_stats) and per-driver paths can't
drift apart. No DB, no network."""
from datetime import datetime, timedelta

from utils.enforcement import MIN_GAP_DAYS, REQUIRED_PER_WEEK, compliance_verdict

NOW = datetime(2026, 7, 1, 12, 0, 0)


def test_quota_met():
    ok, reason = compliance_verdict(REQUIRED_PER_WEEK, NOW - timedelta(days=1), now=NOW)
    assert ok and reason == "weekly quota met"


def test_quota_met_ignores_last_pti_age():
    ok, _ = compliance_verdict(REQUIRED_PER_WEEK + 3, None, now=NOW)
    assert ok


def test_never_submitted():
    ok, reason = compliance_verdict(0, None, now=NOW)
    assert not ok and reason == "no PTI submitted yet this week"


def test_recent_pti_within_gap_is_compliant():
    ok, reason = compliance_verdict(1, NOW - timedelta(days=1), now=NOW)
    assert ok and reason == f"next PTI due in {MIN_GAP_DAYS - 1} day(s)"


def test_gap_elapsed_and_under_quota_is_non_compliant():
    ok, reason = compliance_verdict(1, NOW - timedelta(days=MIN_GAP_DAYS), now=NOW)
    assert not ok
    assert reason == f"only 1/{REQUIRED_PER_WEEK} PTIs submitted this week"


def test_old_last_pti_previous_week():
    ok, reason = compliance_verdict(0, NOW - timedelta(days=10), now=NOW)
    assert not ok
    assert reason == f"only 0/{REQUIRED_PER_WEEK} PTIs submitted this week"
