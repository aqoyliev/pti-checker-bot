"""The scoring must reproduce the reports the fleet has already been shown.

Every expectation below is a row copied out of the 6 Aug 2026 gurman driver
report, so a change that moves a published number fails here.
"""

from utils.report_scoring import (
    COMPLETE,
    NOT_A_PTI,
    PARTIAL,
    REAL,
    classify,
    score_inspection,
)


def payload(missing=(), not_visible=(), fe=False, hood=False):
    data = {
        "missing_areas": list(missing),
        "what_was_not_visible": list(not_visible),
        "fire_extinguisher_shown": fe,
        "checked_clean": ["Under hood"] if hood else [],
    }
    return data


def test_complete_without_extinguisher_scores_95():
    # "17 Jul 95% Complete 8/8 PASS -- - fire extinguisher not shown"
    s = score_inspection(payload())
    assert (s.score, s.filmed, s.required, s.klass) == (95, 8, 8, COMPLETE)


def test_complete_with_extinguisher_scores_100():
    # "Gandra Ferreira, Ru 1286 1 1 100%"
    assert score_inspection(payload(fe=True)).score == 100


def test_under_hood_joins_the_required_set_when_filmed():
    # "01 Jul 76% Real 7/9 FAIL Lights, Windshield - fire extinguisher not shown"
    s = score_inspection(payload(missing=["Lights", "Windshield"], hood=True))
    assert (s.score, s.filmed, s.required, s.klass) == (76, 7, 9, REAL)
    # "12 Jul 95% Complete 9/9 PASS"
    assert score_inspection(payload(hood=True)).filmed == 9


def test_extinguisher_is_worth_five_points_and_never_the_verdict():
    # "28 Jul 89% Real 7/8 FAIL ABS lamp"  (no "not shown" note => shown)
    shown = score_inspection(payload(missing=["ABS lamp"], fe=True))
    hidden = score_inspection(payload(missing=["ABS lamp"], fe=False))
    assert (shown.score, hidden.score) == (89, 84)
    assert shown.klass == hidden.klass == REAL


def test_each_not_visible_item_costs_two_points():
    # "27 Jul 57% Partial 5/8 FAIL Brake pads, Air lines, ABS lamp
    #  - fire extinguisher not shown; air lines on catwalk; driver-side steer
    #  tire; passenger-side mirror"
    s = score_inspection(
        payload(
            missing=["Brake pads", "Air lines", "ABS lamp"],
            not_visible=["air lines on catwalk", "driver-side steer tire",
                         "passenger-side mirror"],
        )
    )
    assert (s.score, s.filmed, s.klass) == (57, 5, PARTIAL)


def test_not_visible_deduction_floors_at_zero():
    s = score_inspection(payload(not_visible=[f"item {i}" for i in range(9)]))
    assert s.score == 85


def test_nothing_filmed_still_earns_the_detail_points():
    # "01 Jul 10% Not a PTI 0/8 FAIL <all eight>"
    s = score_inspection(payload(missing=[
        "Brake pads", "Lights", "Tires", "Mirrors",
        "Windshield", "Air lines", "Frame", "ABS lamp",
    ]))
    assert (s.score, s.filmed, s.klass) == (10, 0, NOT_A_PTI)


def test_one_area_filmed_scores_21():
    # "02 Jul 21% Not a PTI 1/8 FAIL <seven>"
    s = score_inspection(payload(missing=[
        "Brake pads", "Lights", "Tires", "Mirrors",
        "Air lines", "Frame", "ABS lamp",
    ]))
    assert (s.score, s.filmed) == (21, 1)


def test_unknown_area_labels_are_ignored_not_counted():
    s = score_inspection(payload(missing=["Brake pads", "Coffee holder"]))
    assert s.missing == ["Brake pads"]
    assert s.filmed == 7


def test_unreadable_payload_is_not_evidence_of_a_walkaround():
    for bad in (None, "", "{not json", "[]"):
        s = score_inspection(bad)
        assert s.filmed == 0
        assert s.is_real is False
        assert s.klass == NOT_A_PTI


def test_real_is_at_most_two_areas_unfilmed():
    assert classify(0) == COMPLETE
    assert classify(1) == classify(2) == REAL
    assert classify(3) == classify(5) == PARTIAL
    assert classify(6) == NOT_A_PTI
    assert score_inspection(payload(missing=["Lights", "Tires"])).is_real
    assert not score_inspection(payload(missing=["Lights", "Tires", "Frame"])).is_real
