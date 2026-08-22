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


def test_no_model_without_a_working_key_is_offered():
    # gemini-3-pro-preview 404s on every production key, so selecting it could
    # only fail. 2.5-pro is different: it 404s on the newer keys but still works
    # on the grandfathered one, and _call_gemini_with_retry only abandons a model
    # once every key has refused it. See utils/gemini.py's note on 2026-08-20.
    assert "gemini-3-pro-preview" not in gemini.AVAILABLE_GEMINI_MODELS
    assert "gemini-2.5-flash-lite" not in gemini.AVAILABLE_GEMINI_MODELS


def test_a_fallback_sits_after_the_default():
    # If the grandfathered key ever loses 2.5 access the bot has to land
    # somewhere, so the default must not be the only entry.
    assert gemini.AVAILABLE_GEMINI_MODELS[1:] != ()


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
