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


def test_format_result_fail_with_driver_and_oos_issue():
    data = {"status": "FAIL", "severity": "CRITICAL",
            "issues": [{"text": "Exposed tire cords", "evidence": "x" * 30, "oos": True}]}
    text = pp.format_result(data, photos=1, videos=0, driver_name="John Doe")
    assert "PTI Result: FAIL" in text
    assert "John Doe" in text
    assert "Out-of-service" in text
    assert "Exposed tire cords" in text
    assert "📎 Checked: 1 photo" in text


def test_format_result_pass_shows_advisory_for_non_oos_issue():
    data = {"status": "PASS", "severity": "MINOR",
            "issues": [{"text": "Mirror cracked", "evidence": "y" * 30, "oos": False}]}
    text = pp.format_result(data)
    assert "PTI Result: PASS" in text
    assert "Advisories" in text
    assert "Mirror cracked" in text
    assert "Out-of-service" not in text


def test_format_result_escapes_html():
    data = {"status": "FAIL", "issues": [{"text": "Bad <tag> & stuff", "evidence": "y" * 30, "oos": True}]}
    text = pp.format_result(data)
    assert "<tag>" not in text
    assert "&lt;tag&gt;" in text


# ---------- apply_oos_verdict ----------

def test_oos_verdict_fails_on_oos_issue():
    data = {"status": "PASS", "severity": "NONE",
            "issues": [{"text": "Flat tire", "evidence": "z" * 30, "oos": True}]}
    assert pp.apply_oos_verdict(data) is True
    assert data["status"] == "FAIL"
    assert data["severity"] == "CRITICAL"


def test_oos_verdict_passes_with_advisory_only():
    data = {"status": "FAIL", "severity": "CRITICAL",
            "issues": [{"text": "Mirror cracked", "evidence": "z" * 30, "oos": False}]}
    assert pp.apply_oos_verdict(data) is False
    assert data["status"] == "PASS"
    assert data["severity"] == "MINOR"


def test_oos_verdict_passes_clean():
    data = {"status": "FAIL", "severity": "MAJOR", "issues": []}
    assert pp.apply_oos_verdict(data) is False
    assert data["status"] == "PASS"
    assert data["severity"] == "NONE"


# ---------- apply_completeness_verdict ----------

def test_completeness_fails_when_required_area_missing():
    data = {"status": "PASS", "severity": "NONE", "checked_clean": ["Tires", "Lights"],
            "missing_areas": ["Brake pads", "Fire extinguisher & triangle"]}
    assert pp.apply_completeness_verdict(data) is True
    assert data["status"] == "FAIL"
    assert data["severity"] == "MAJOR"
    assert data["missing_areas"] == ["Brake pads", "Fire extinguisher & triangle"]
    assert "/check" in data["advice"]


def test_completeness_passes_when_nothing_missing():
    data = {"status": "PASS", "severity": "NONE", "missing_areas": []}
    assert pp.apply_completeness_verdict(data) is False
    assert data["status"] == "PASS"
    assert data["missing_areas"] == []


def test_completeness_ignores_unknown_and_clean_labels():
    # Junk labels and anything already marked clean must not count as missing.
    data = {"status": "PASS", "severity": "NONE", "checked_clean": ["Tires"],
            "missing_areas": ["Tires", "Engine bay", "tires", "Frame"]}
    assert pp.apply_completeness_verdict(data) is True
    assert data["missing_areas"] == ["Frame"]


def test_completeness_safety_net_from_not_visible():
    # Even if the model leaves missing_areas empty, a canonical label in
    # what_was_not_visible still triggers the incomplete FAIL.
    data = {"status": "PASS", "severity": "NONE", "missing_areas": [],
            "what_was_not_visible": ["Brake pads", "Spare fuse"]}
    assert pp.apply_completeness_verdict(data) is True
    assert data["status"] == "FAIL"
    assert data["missing_areas"] == ["Brake pads"]


def test_completeness_keeps_critical_severity_on_oos_fail():
    # An OOS fail that is also incomplete stays CRITICAL, not downgraded to MAJOR.
    data = {"status": "FAIL", "severity": "CRITICAL", "advice": "Fix brakes.",
            "missing_areas": ["Air lines"]}
    assert pp.apply_completeness_verdict(data) is True
    assert data["status"] == "FAIL"
    assert data["severity"] == "CRITICAL"
    assert data["advice"] == "Fix brakes."


def test_format_result_shows_incomplete_section():
    data = {"status": "FAIL", "severity": "MAJOR", "issues": [],
            "checked_clean": ["Tires"], "missing_areas": ["Brake pads"]}
    text = pp.format_result(data)
    assert "PTI Result: FAIL" in text
    assert "Incomplete" in text
    assert "Brake pads" in text


def test_format_result_does_not_repeat_missing_in_not_visible():
    data = {"status": "FAIL", "severity": "MAJOR", "issues": [],
            "missing_areas": ["Brake pads"],
            "what_was_not_visible": ["Brake pads", "Spare fuse"]}
    text = pp.format_result(data)
    # "Brake pads" appears once (the Incomplete section), not in the Not visible line.
    assert "Not visible:</b> Spare fuse" in text
    assert text.count("Brake pads") == 1
