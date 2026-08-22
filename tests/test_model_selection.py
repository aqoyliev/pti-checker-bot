"""Unit tests for the runtime-settable Gemini model (admin model switching).

Pure: no DB / no network. Each test restores the active model so ordering and
other suites are unaffected.

These deliberately avoid hardcoding a model id beyond the registry itself: on
2026-08-20 every ``gemini-2.5-*`` id was retired by Google at once, and tests
that named one directly failed for a reason that had nothing to do with the
behaviour they were checking.
"""
from utils import gemini


def _restore(original):
    gemini.set_active_model(original)


def _other_model() -> str:
    """Any valid model that isn't the default — the thing to switch *to*."""
    return next(m for m in gemini.AVAILABLE_GEMINI_MODELS
                if m != gemini.DEFAULT_GEMINI_MODEL)


def test_default_model_is_first_available():
    assert gemini.DEFAULT_GEMINI_MODEL == gemini.AVAILABLE_GEMINI_MODELS[0]


def test_there_is_more_than_one_choice():
    # The admin panel offers a switch, and model failover needs somewhere to go.
    assert len(gemini.AVAILABLE_GEMINI_MODELS) > 1


def test_no_retired_model_is_offered():
    # A model id the API 404s on must not be selectable: offering it just moves
    # the outage one tap away. See utils/gemini.py's note on 2026-08-20.
    assert not [m for m in gemini.AVAILABLE_GEMINI_MODELS
                if m.startswith("gemini-2.5-") or m == "gemini-3-pro-preview"]


def test_set_active_model_switches_when_valid():
    original = gemini.get_active_model()
    target = _other_model()
    try:
        assert gemini.set_active_model(target) is True
        assert gemini.get_active_model() == target
    finally:
        _restore(original)


def test_set_active_model_rejects_unknown_and_keeps_current():
    original = gemini.get_active_model()
    target = _other_model()
    try:
        gemini.set_active_model(target)
        assert gemini.set_active_model("not-a-real-model") is False
        # Unchanged from the last valid value, not silently broken.
        assert gemini.get_active_model() == target
    finally:
        _restore(original)
