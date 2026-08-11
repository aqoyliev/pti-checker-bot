"""Unit tests for the runtime-settable Gemini model (admin model switching).

Pure: no DB / no network. Each test restores the active model so ordering and
other suites are unaffected.
"""
from utils import gemini


def _restore(original):
    gemini.set_active_model(original)


def test_default_model_is_first_available():
    assert gemini.DEFAULT_GEMINI_MODEL == gemini.AVAILABLE_GEMINI_MODELS[0]


def test_flash_is_a_choice():
    assert "gemini-2.5-flash" in gemini.AVAILABLE_GEMINI_MODELS


def test_set_active_model_switches_when_valid():
    original = gemini.get_active_model()
    try:
        assert gemini.set_active_model("gemini-2.5-flash") is True
        assert gemini.get_active_model() == "gemini-2.5-flash"
    finally:
        _restore(original)


def test_set_active_model_rejects_unknown_and_keeps_current():
    original = gemini.get_active_model()
    try:
        gemini.set_active_model("gemini-2.5-flash")
        assert gemini.set_active_model("not-a-real-model") is False
        # Unchanged from the last valid value, not silently broken.
        assert gemini.get_active_model() == "gemini-2.5-flash"
    finally:
        _restore(original)
