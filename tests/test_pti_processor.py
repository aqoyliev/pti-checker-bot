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


def test_filter_banned_phrase_checks_evidence_not_title():
    # A concrete worn-tire finding whose TITLE reads "Worn trailer tire tread" (a banned
    # phrase) must survive when the EVIDENCE is a concrete observation — the gate judges
    # evidence, not the title. Regression: such findings were being wrongly dropped.
    data = {"status": "FAIL", "severity": "MINOR", "issues": [_issue(
        "(2:40) Worn trailer tire tread",
        "The center tread grooves on the outer trailer dual are almost completely smooth, "
        "while the adjacent inner dual still shows deep grooves.")]}
    dropped = pp.filter_hallucinated_issues(data)
    assert dropped == 0
    assert len(data["issues"]) == 1


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
    assert "❌ <b>Exposed tire cords</b>" in text    # OOS defect rendered inline, bold, with ❌
    assert "📎 Checked: 1 photo" in text


def test_format_result_pass_shows_advisory_for_non_oos_issue():
    data = {"status": "PASS", "severity": "MINOR",
            "issues": [{"text": "Mirror cracked", "evidence": "y" * 30, "oos": False}]}
    text = pp.format_result(data)
    assert "PTI Result: PASS" in text
    assert "⚠️ Mirror cracked" in text              # advisory rendered inline with ⚠️
    assert "❌ Mirror cracked" not in text           # not flagged out-of-service


def test_format_result_escapes_html():
    data = {"status": "FAIL", "issues": [{"text": "Bad <tag> & stuff", "evidence": "y" * 30, "oos": True}]}
    text = pp.format_result(data)
    assert "<tag>" not in text
    assert "&lt;tag&gt;" in text


# ---------- has_oos_defect (label only, never fails the verdict) ----------

def test_has_oos_defect_true_when_oos_issue_present():
    data = {"issues": [{"text": "Flat tire", "evidence": "z" * 30, "oos": True}]}
    assert pp.has_oos_defect(data) is True


def test_has_oos_defect_false_for_advisory_only():
    data = {"issues": [{"text": "Mirror cracked", "evidence": "z" * 30, "oos": False}]}
    assert pp.has_oos_defect(data) is False


def test_oos_defect_does_not_fail_a_complete_inspection():
    # OOS no longer drives the verdict: a complete inspection PASSES even with an
    # out-of-service defect. The defect is still reported; severity stays CRITICAL.
    data = {"status": "FAIL", "severity": "NONE", "missing_areas": [],
            "checked_clean": ["ABS lamp"],
            "issues": [{"text": "Flat tire", "evidence": "z" * 30, "oos": True}]}
    assert pp.apply_completeness_verdict(data) is False
    assert data["status"] == "PASS"
    assert data["severity"] == "CRITICAL"


def test_complete_clean_inspection_passes():
    data = {"status": "FAIL", "severity": "MAJOR", "issues": [], "missing_areas": [],
            "checked_clean": ["ABS lamp"]}
    assert pp.apply_completeness_verdict(data) is False
    assert data["status"] == "PASS"
    assert data["severity"] == "NONE"


# ---------- apply_completeness_verdict ----------

def test_completeness_fails_when_required_area_missing():
    data = {"status": "PASS", "severity": "NONE", "checked_clean": ["Tires", "Lights", "ABS lamp"],
            "missing_areas": ["Brake pads", "Air lines"]}
    assert pp.apply_completeness_verdict(data) is True
    assert data["status"] == "FAIL"
    assert data["severity"] == "MAJOR"
    assert data["missing_areas"] == ["Brake pads", "Air lines"]
    assert data["advice"] == ""


def test_completeness_ignores_fire_extinguisher_as_missing_area():
    # Fire extinguisher & triangle is no longer a required/completeness area —
    # an un-filmed report for it must not trigger an incomplete FAIL.
    data = {"status": "PASS", "severity": "NONE", "checked_clean": ["ABS lamp"],
            "missing_areas": ["Fire extinguisher & triangle"]}
    assert pp.apply_completeness_verdict(data) is False
    assert data["status"] == "PASS"
    assert data["missing_areas"] == []


def test_completeness_passes_when_nothing_missing():
    data = {"status": "PASS", "severity": "NONE", "missing_areas": [], "checked_clean": ["ABS lamp"]}
    assert pp.apply_completeness_verdict(data) is False
    assert data["status"] == "PASS"
    assert data["missing_areas"] == []


def test_completeness_ignores_unknown_and_clean_labels():
    # Junk labels and anything already marked clean must not count as missing.
    data = {"status": "PASS", "severity": "NONE", "checked_clean": ["Tires", "ABS lamp"],
            "missing_areas": ["Tires", "Engine bay", "tires", "Frame"]}
    assert pp.apply_completeness_verdict(data) is True
    assert data["missing_areas"] == ["Frame"]


def test_completeness_safety_net_from_not_visible():
    # Even if the model leaves missing_areas empty, a canonical label in
    # what_was_not_visible still triggers the incomplete FAIL.
    data = {"status": "PASS", "severity": "NONE", "missing_areas": [],
            "checked_clean": ["ABS lamp"],
            "what_was_not_visible": ["Brake pads", "Spare fuse"]}
    assert pp.apply_completeness_verdict(data) is True
    assert data["status"] == "FAIL"
    assert data["missing_areas"] == ["Brake pads"]


def test_completeness_incomplete_with_oos_is_critical_fail():
    # Incomplete + an OOS defect → FAIL at CRITICAL severity. The FAIL is from
    # incompleteness; the OOS defect only raises the severity for awareness.
    data = {"status": "PASS", "severity": "NONE", "checked_clean": ["ABS lamp"],
            "issues": [{"text": "Flat tire", "evidence": "z" * 30, "oos": True}],
            "missing_areas": ["Air lines"]}
    assert pp.apply_completeness_verdict(data) is True
    assert data["status"] == "FAIL"
    assert data["severity"] == "CRITICAL"


# ---------- ABS lamp is a required area ----------

def test_abs_lamp_not_filmed_fails_as_incomplete():
    # ABS lamp neither in checked_clean nor mentioned in an issue → required area
    # missing → FAIL, even though the model didn't list it in missing_areas.
    data = {"status": "PASS", "severity": "NONE", "missing_areas": [],
            "checked_clean": ["Tires", "Lights", "Brake pads"]}
    assert pp.apply_completeness_verdict(data) is True
    assert data["status"] == "FAIL"
    assert "ABS lamp" in data["missing_areas"]


def test_abs_lamp_filmed_off_passes():
    # ABS lamp filmed and off → in checked_clean → not missing.
    data = {"status": "PASS", "severity": "NONE", "missing_areas": [],
            "checked_clean": ["ABS lamp"]}
    assert pp.apply_completeness_verdict(data) is False
    assert "ABS lamp" not in data["missing_areas"]


def test_abs_lamp_on_as_issue_is_not_missing():
    # ABS lamp filmed and lit → reported as an issue → counts as accounted-for.
    data = {"status": "PASS", "severity": "NONE", "missing_areas": [],
            "issues": [{"text": "(1:58) ABS malfunction lamp on (49 CFR 393.55)",
                        "evidence": "z" * 30, "oos": True}]}
    assert pp.apply_completeness_verdict(data) is False
    assert "ABS lamp" not in data["missing_areas"]
    assert data["advice"] == ""


def test_format_result_unfilmed_areas_go_to_not_visible():
    data = {"status": "FAIL", "severity": "MAJOR", "issues": [],
            "checked_clean": ["Tires"], "missing_areas": ["Brake pads"]}
    text = pp.format_result(data)
    assert "PTI Result: FAIL" in text
    assert "✅ Tires" in text                        # clean area shown inline
    assert "❌ Brake pads" not in text               # not a ❌ row anymore
    assert "Not visible:</b> Brake pads" in text     # folded into the one line
    assert "💡" in text                              # re-film advice present


def test_format_result_incomplete_with_oos_keeps_do_not_drive_and_refilm():
    # PASS-on-OOS means the advice line is the only strong signal, so an incomplete +
    # out-of-service result must show BOTH the do-not-drive warning and the re-film prompt.
    data = {"status": "PASS", "severity": "NONE",
            "checked_clean": ["Lights"],
            "issues": [{"text": "(0:55) ABS lamp on", "evidence": "z" * 30, "oos": True}],
            "missing_areas": ["Brake pads"],
            "advice": "Do not drive — out-of-service defect."}
    pp.apply_completeness_verdict(data)
    assert data["status"] == "FAIL"
    text = pp.format_result(data)
    assert "Do not drive" in text
    assert "Re-film the missing areas" in text


def test_format_result_missing_and_not_visible_merge_deduped():
    data = {"status": "FAIL", "severity": "MAJOR", "issues": [],
            "missing_areas": ["Brake pads"],
            "what_was_not_visible": ["Brake pads", "Spare fuse"]}
    text = pp.format_result(data)
    # Missing required areas + free-text notes share one line, with no duplicate.
    assert "Not visible:</b> Brake pads, Spare fuse" in text
    assert text.count("Brake pads") == 1


# ---------- components checklist ----------

def test_format_result_lists_clean_areas_inline():
    from html import escape
    data = {"status": "PASS", "severity": "NONE", "issues": [],
            "checked_clean": list(pp.REQUIRED_AREAS)}
    text = pp.format_result(data)
    for area in pp.REQUIRED_AREAS:
        assert f"✅ {escape(area)}" in text


def test_format_result_defect_and_clean_and_missing_render_distinctly():
    data = {"status": "FAIL", "severity": "MAJOR",
            "issues": [{"text": "Tires", "evidence": "x" * 30, "oos": False}],
            "checked_clean": ["Lights"],
            "missing_areas": ["Brake pads"]}
    text = pp.format_result(data)
    assert "✅ Lights" in text                        # clean
    assert "⚠️ Tires" in text                         # advisory defect
    assert "Not visible:</b> Brake pads" in text      # unfilmed
    assert "❌ Brake pads" not in text


def test_format_result_checklist_excludes_fire_extinguisher():
    # Fire extinguisher & triangle is no longer a completeness area, so it must
    # not appear in the canonical components checklist.
    data = {"status": "PASS", "severity": "NONE", "checked_clean": list(pp.REQUIRED_AREAS)}
    text = pp.format_result(data)
    assert "Fire extinguisher" not in text


def test_under_hood_is_optional_not_required():
    # "Under hood" must never gate completeness: even if the model marks it missing,
    # the inspection still PASSes and it is dropped from the normalized missing list.
    assert "Under hood" not in pp.REQUIRED_AREAS
    assert "Under hood" in pp.OPTIONAL_AREAS
    data = {"status": "PASS", "severity": "NONE", "issues": [],
            "checked_clean": [a for a in pp.REQUIRED_AREAS],
            "missing_areas": ["Under hood"]}
    incomplete = pp.apply_completeness_verdict(data)
    assert incomplete is False
    assert data["status"] == "PASS"
    assert data["missing_areas"] == []


def test_format_result_optional_area_shown_only_when_clean():
    # An optional area (Under hood) shows as ✅ when filmed clean, is omitted when not,
    # and is never rendered as a ❌.
    clean = pp.format_result({"status": "PASS", "severity": "NONE", "issues": [],
                              "checked_clean": list(pp.REQUIRED_AREAS) + ["Under hood"]})
    assert "✅ Under hood" in clean
    not_filmed = pp.format_result({"status": "PASS", "severity": "NONE", "issues": [],
                                   "checked_clean": list(pp.REQUIRED_AREAS)})
    assert "Under hood" not in not_filmed
    assert "❌ Under hood" not in not_filmed


# ---------- merge_tire_pass (focused tire-only second pass) ----------

def _tire_issue(oos=False):
    return {
        "text": "(2:18) Trailer driver-side rear inner dual: center tread worn smooth (49 CFR 393.75)",
        "evidence": "Inner dual center tread is smooth and featureless with no visible "
                    "grooves, while the adjacent outer dual still shows deep grooves.",
        "oos": oos,
    }


def test_merge_tire_pass_promotes_worn_tire_as_advisory():
    # The broad pass marked Tires clean; the focused pass found a clearly worn tire.
    # It MUST be surfaced (driver replaces it) but as an advisory, never OOS.
    data = {"issues": [], "checked_clean": ["Lights", "Tires"], "missing_areas": []}
    tire = {"tire_defect": True, "issues": [_tire_issue(oos=False)]}
    assert pp.merge_tire_pass(data, tire) == 1
    assert "Tires" not in data["checked_clean"]          # no longer "clean"
    assert "Tires" not in data.get("missing_areas", [])  # nor counted "missing"
    promoted = data["issues"][-1]
    assert promoted["oos"] is False                      # advisory, not OOS
    # Survives the evidence gate; a complete inspection with only an advisory is a
    # non-critical PASS (worn tire shown, but not out-of-service).
    assert pp.filter_hallucinated_issues(data) == 0
    pp.apply_completeness_verdict(data)
    assert pp.has_oos_defect(data) is False
    assert data["severity"] != "CRITICAL"


def test_merge_tire_pass_forces_advisory_even_if_model_says_oos():
    # Company policy: worn/bald tread is never OOS. A model-labelled oos=True finding
    # must be downgraded on promotion.
    data = {"issues": [], "checked_clean": ["Tires"]}
    assert pp.merge_tire_pass(data, {"tire_defect": True, "issues": [_tire_issue(oos=True)]}) == 1
    assert data["issues"][-1]["oos"] is False


def test_merge_tire_pass_noop_when_no_defect():
    data = {"issues": [], "checked_clean": ["Tires"]}
    assert pp.merge_tire_pass(data, {"tire_defect": False, "issues": []}) == 0
    assert pp.merge_tire_pass(data, None) == 0
    assert "Tires" in data["checked_clean"]


def test_merge_tire_pass_caps_to_avoid_flooding():
    data = {"issues": [], "checked_clean": ["Tires"]}
    many = {"tire_defect": True, "issues": [
        {"text": f"({i}:00) inner dual worn smooth", "evidence": "z" * 30, "oos": False}
        for i in range(6)
    ]}
    promoted = pp.merge_tire_pass(data, many)
    assert promoted == pp._MAX_TIRE_PROMOTE
    assert len(data["issues"]) == pp._MAX_TIRE_PROMOTE


def test_merge_tire_pass_no_double_report():
    # If the broad pass already flagged a tire, don't add a second tire issue.
    data = {"issues": [{"text": "(1:00) Trailer tire flat", "evidence": "y" * 30, "oos": True}],
            "checked_clean": []}
    assert pp.merge_tire_pass(data, {"tire_defect": True, "issues": [_tire_issue()]}) == 0
    assert len(data["issues"]) == 1


# ---------- finalize_result (filter -> merge -> filter -> verdict ordering) ----------

def test_finalize_promotes_tire_when_broad_tire_issue_is_hallucinated():
    # Regression: the broad pass emits a VAGUE tire issue (mentions "tire", so it would
    # trip merge's no-double-report guard) that the evidence gate drops. Filtering must
    # happen BEFORE the merge so the real tire-pass finding still gets promoted — i.e.
    # the worn tire must survive to the driver, not vanish.
    data = {
        "status": "PASS", "severity": "NONE",
        "issues": [{"text": "Drive tires show wear", "evidence": "worn tires"}],  # banned -> dropped
        "checked_clean": ["Tires"], "missing_areas": [],
    }
    tire = {"tire_defect": True, "issues": [_tire_issue(oos=False)]}
    promoted = pp.finalize_result(data, tire)
    assert promoted == 1
    texts = " ".join(i["text"] for i in data["issues"]).lower()
    assert "worn smooth" in texts          # the real tire-pass finding survived
    assert "show wear" not in texts        # the hallucinated broad issue was dropped
    assert all(i["oos"] is False for i in data["issues"])
    assert "Tires" not in data["checked_clean"]


def test_finalize_no_tire_pass_is_noop_filter_and_verdict():
    data = {"status": "FAIL", "issues": [], "checked_clean": ["Tires"],
            "missing_areas": ["Brake pads"]}
    assert pp.finalize_result(data, None) == 0
    assert data["status"] == "FAIL"        # still incomplete -> FAIL
    assert "Brake pads" in data["missing_areas"]
