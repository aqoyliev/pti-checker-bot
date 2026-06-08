"""Unit tests for the pure (no secrets / no network) helpers in pti_processor.

These cover the result-formatting and hallucination-filtering logic that decides
what drivers actually see, plus the small formatting helpers.
"""
from utils import pti_processor as pp


# ---------- _fmt_timestamp ----------

def test_fmt_timestamp_formats_minutes_and_seconds():
    assert pp._fmt_timestamp(0) == "0:00"
    assert pp._fmt_timestamp(9) == "0:09"
    assert pp._fmt_timestamp(65) == "1:05"
    assert pp._fmt_timestamp(125) == "2:05"


# ---------- _media_summary ----------

def test_media_summary_none_when_empty():
    assert pp._media_summary(0, 0) is None


def test_media_summary_pluralizes():
    assert pp._media_summary(1, 0) == "📎 Checked: 1 photo"
    assert pp._media_summary(2, 0) == "📎 Checked: 2 photos"
    assert pp._media_summary(0, 1) == "📎 Checked: 1 video"
    assert pp._media_summary(2, 3) == "📎 Checked: 2 photos and 3 videos"


# ---------- filter_hallucinated_issues ----------

def _issue(text, evidence):
    return {"text": text, "evidence": evidence}


def test_filter_keeps_concrete_issue():
    data = {
        "status": "FAIL",
        "severity": "MAJOR",
        "issues": [_issue("Left headlight broken", "The left headlight glass is shattered and the bulb is dark.")],
    }
    dropped = pp.filter_hallucinated_issues(data)
    assert dropped == 0
    assert len(data["issues"]) == 1
    assert data["status"] == "FAIL"


def test_filter_drops_short_evidence():
    data = {"status": "FAIL", "severity": "MINOR", "issues": [_issue("Something", "too short")]}
    dropped = pp.filter_hallucinated_issues(data)
    assert dropped == 1
    assert data["issues"] == []


def test_filter_drops_banned_phrase():
    data = {"status": "FAIL", "severity": "MAJOR",
            "issues": [_issue("Tires", "There is visible tire wear across the drive axle in this frame.")]}
    dropped = pp.filter_hallucinated_issues(data)
    assert dropped == 1
    assert data["issues"] == []


def test_filter_flips_to_pass_when_all_dropped():
    data = {
        "status": "FAIL",
        "severity": "CRITICAL",
        "advice": "Replace the tires immediately.",
        "issues": [_issue("Tires", "Outer shoulder is smooth and the tread is low on this tire.")],
    }
    dropped = pp.filter_hallucinated_issues(data)
    assert dropped == 1
    assert data["status"] == "PASS"
    assert data["severity"] == "NONE"
    assert data["advice"] == ""


# ---------- format_result ----------

def test_format_result_pass():
    text = pp.format_result({"status": "PASS", "severity": "NONE", "issues": []})
    assert "✅" in text
    assert "PTI Result: PASS" in text


def test_format_result_fail_with_driver_and_issue():
    data = {"status": "FAIL", "severity": "MAJOR",
            "issues": [{"text": "Brake pad worn", "evidence": "x" * 30}]}
    text = pp.format_result(data, photos=1, videos=0, driver_name="John Doe")
    assert "PTI Result: FAIL" in text
    assert "John Doe" in text
    assert "Brake pad worn" in text
    assert "📎 Checked: 1 photo" in text


def test_format_result_escapes_html():
    data = {"status": "FAIL", "issues": [{"text": "Bad <tag> & stuff", "evidence": "y" * 30}]}
    text = pp.format_result(data)
    assert "<tag>" not in text
    assert "&lt;tag&gt;" in text
