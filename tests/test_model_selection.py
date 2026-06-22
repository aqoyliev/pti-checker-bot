"""Unit tests for the runtime-settable Gemini model (admin model switching).

Pure: no DB / no network. Each test restores the active model so ordering and
other suites are unaffected.
"""
import test_pti


def _restore(original):
    test_pti.set_active_model(original)


def test_default_model_is_first_available():
    assert test_pti.DEFAULT_GEMINI_MODEL == test_pti.AVAILABLE_GEMINI_MODELS[0]


def test_flash_is_a_choice():
    assert "gemini-2.5-flash" in test_pti.AVAILABLE_GEMINI_MODELS


def test_set_active_model_switches_when_valid():
    original = test_pti.get_active_model()
    try:
        assert test_pti.set_active_model("gemini-2.5-flash") is True
        assert test_pti.get_active_model() == "gemini-2.5-flash"
    finally:
        _restore(original)


def test_set_active_model_rejects_unknown_and_keeps_current():
    original = test_pti.get_active_model()
    try:
        test_pti.set_active_model("gemini-2.5-flash")
        assert test_pti.set_active_model("not-a-real-model") is False
        # Unchanged from the last valid value, not silently broken.
        assert test_pti.get_active_model() == "gemini-2.5-flash"
    finally:
        _restore(original)
