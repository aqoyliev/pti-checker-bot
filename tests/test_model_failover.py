"""Model failover: a retired (404) model must not take PTI checking down.

The 2026-08-20 outage in one sentence: every ``gemini-2.5-*`` id started
returning 404, 404 is neither transient nor an overload, so it propagated
straight out of the key-failover loop and every inspection errored for ~2 days.

Pure: no network. The Gemini call is a stub that raises whatever we want.
"""
import asyncio

import pytest
from google.genai import errors as genai_errors

from utils import gemini, pti_processor


class _FakeResponse:
    """Minimal stand-in for the httpx response genai errors carry."""

    def __init__(self, code):
        self.status_code = code
        self.headers = {}
        self.text = ""

    def json(self):
        return {}


def _client_error(code: int, message: str) -> genai_errors.ClientError:
    return genai_errors.ClientError(
        code, {"error": {"code": code, "message": message}}, _FakeResponse(code))


RETIRED = ("This model models/gemini-2.5-pro is no longer available to new "
           "users. Please update your code to use models/gemini-3.1-pro-preview")


@pytest.fixture(autouse=True)
def _restore_model():
    original = gemini.get_active_model()
    yield
    gemini.set_active_model(original)


@pytest.fixture(autouse=True)
def _one_key(monkeypatch):
    # Key failover is a separate axis; pin it to one key so these tests only
    # exercise the model axis.
    monkeypatch.setattr(pti_processor, "get_api_keys", lambda: ["k1"])


def test_retired_model_switches_to_the_next_one():
    gemini.set_active_model(gemini.DEFAULT_GEMINI_MODEL)
    seen = []

    def fn(*args, api_key=None, **kwargs):
        model = gemini.get_active_model()
        seen.append(model)
        if model == gemini.DEFAULT_GEMINI_MODEL:
            raise _client_error(404, RETIRED)
        return "ok"

    assert asyncio.run(pti_processor._call_gemini_with_retry(fn)) == "ok"
    # It moved off the dead model rather than giving up.
    assert seen[0] == gemini.DEFAULT_GEMINI_MODEL
    assert gemini.get_active_model() != gemini.DEFAULT_GEMINI_MODEL


def test_every_model_retired_finally_raises():
    def fn(*args, api_key=None, **kwargs):
        raise _client_error(404, RETIRED)

    with pytest.raises(genai_errors.ClientError):
        asyncio.run(pti_processor._call_gemini_with_retry(fn))


def test_each_model_is_tried_only_once():
    tried = []

    def fn(*args, api_key=None, **kwargs):
        tried.append(gemini.get_active_model())
        raise _client_error(404, RETIRED)

    with pytest.raises(genai_errors.ClientError):
        asyncio.run(pti_processor._call_gemini_with_retry(fn))
    assert len(tried) == len(set(tried))
    assert len(tried) == len(gemini.AVAILABLE_GEMINI_MODELS)


def test_a_404_that_is_not_about_a_model_still_propagates():
    # Only a retired *model* may trigger a model switch; any other 404 is a real
    # error and must surface instead of silently walking the registry.
    def fn(*args, api_key=None, **kwargs):
        raise _client_error(404, "file not found")

    before = gemini.get_active_model()
    with pytest.raises(genai_errors.ClientError):
        asyncio.run(pti_processor._call_gemini_with_retry(fn))
    assert gemini.get_active_model() == before


def test_overload_does_not_change_the_model():
    # A 429/503 is transient and model-independent -- switching models on it
    # would wander off the admin's chosen model for a passing cloud.
    def fn(*args, api_key=None, **kwargs):
        raise _client_error(429, "rate limited")

    before = gemini.get_active_model()
    with pytest.raises(genai_errors.ClientError):
        asyncio.run(pti_processor._call_gemini_with_retry(fn))
    assert gemini.get_active_model() == before
