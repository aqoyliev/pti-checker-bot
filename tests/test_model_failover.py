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


@pytest.fixture
def _one_key(monkeypatch):
    # Key failover is a separate axis; pin it to one key so these tests only
    # exercise the model axis.
    monkeypatch.setattr(pti_processor, "get_api_keys", lambda: ["k1"])


@pytest.fixture
def _four_keys(monkeypatch):
    monkeypatch.setattr(pti_processor, "get_api_keys",
                        lambda: ["dead1", "dead2", "dead3", "grandfathered"])


def test_a_404_on_one_key_still_tries_the_others(_four_keys):
    """The whole point of keeping gemini-2.5-pro as the default.

    Google retires a model per *account*: it 404s on the three newer keys and
    works on the grandfathered one. Abandoning the model on the first 404 would
    strand the fleet on a costlier model while a working key sat untried.
    """
    gemini.set_active_model(gemini.DEFAULT_GEMINI_MODEL)
    tried = []

    def fn(*args, api_key=None, **kwargs):
        tried.append(api_key)
        if api_key != "grandfathered":
            raise _client_error(404, RETIRED)
        return "ok"

    assert asyncio.run(pti_processor._call_gemini_with_retry(fn)) == "ok"
    assert tried == ["dead1", "dead2", "dead3", "grandfathered"]
    # and it stayed on the model the prompts are tuned for
    assert gemini.get_active_model() == gemini.DEFAULT_GEMINI_MODEL


def test_a_key_that_cannot_serve_the_model_is_remembered(_four_keys):
    """Learned once, not re-discovered on every inspection.

    It also protects the frame split: _run_split_passes hands one chunk per key,
    so a key that 404s costs that slice of the walkaround from the merged
    completeness verdict -- silently, since a partial merge still returns.
    """
    pti_processor._model_dead_keys.clear()
    gemini.set_active_model(gemini.DEFAULT_GEMINI_MODEL)

    def fn(*args, api_key=None, **kwargs):
        if api_key != "grandfathered":
            raise _client_error(404, RETIRED)
        return "ok"

    asyncio.run(pti_processor._call_gemini_with_retry(fn))
    viable = pti_processor._viable_keys(
        gemini.DEFAULT_GEMINI_MODEL,
        ["dead1", "dead2", "dead3", "grandfathered"])
    assert viable == ["grandfathered"]
    pti_processor._model_dead_keys.clear()


def test_viable_keys_never_returns_empty():
    # With every key ruled out the caller still needs one attempt, or the model
    # failover would never get to observe a failure and move on.
    pti_processor._model_dead_keys.clear()
    for k in ("a", "b"):
        pti_processor._note_key_cannot_serve("m", k)
    assert pti_processor._viable_keys("m", ["a", "b"]) == ["a", "b"]
    pti_processor._model_dead_keys.clear()


def test_model_is_abandoned_only_when_every_key_refuses_it(_four_keys):
    gemini.set_active_model(gemini.DEFAULT_GEMINI_MODEL)

    def fn(*args, api_key=None, **kwargs):
        if gemini.get_active_model() == gemini.DEFAULT_GEMINI_MODEL:
            raise _client_error(404, RETIRED)
        return "ok"

    assert asyncio.run(pti_processor._call_gemini_with_retry(fn)) == "ok"
    assert gemini.get_active_model() != gemini.DEFAULT_GEMINI_MODEL


def test_retired_model_switches_to_the_next_one(_one_key):
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


def test_every_model_retired_finally_raises(_one_key):
    def fn(*args, api_key=None, **kwargs):
        raise _client_error(404, RETIRED)

    with pytest.raises(genai_errors.ClientError):
        asyncio.run(pti_processor._call_gemini_with_retry(fn))


def test_each_model_is_tried_only_once(_one_key):
    tried = []

    def fn(*args, api_key=None, **kwargs):
        tried.append(gemini.get_active_model())
        raise _client_error(404, RETIRED)

    with pytest.raises(genai_errors.ClientError):
        asyncio.run(pti_processor._call_gemini_with_retry(fn))
    assert len(tried) == len(set(tried))
    assert len(tried) == len(gemini.AVAILABLE_GEMINI_MODELS)


def test_a_404_that_is_not_about_a_model_still_propagates(_one_key):
    # Only a retired *model* may trigger a model switch; any other 404 is a real
    # error and must surface instead of silently walking the registry.
    def fn(*args, api_key=None, **kwargs):
        raise _client_error(404, "file not found")

    before = gemini.get_active_model()
    with pytest.raises(genai_errors.ClientError):
        asyncio.run(pti_processor._call_gemini_with_retry(fn))
    assert gemini.get_active_model() == before


def test_overload_does_not_change_the_model(_one_key):
    # A 429/503 is transient and model-independent -- switching models on it
    # would wander off the admin's chosen model for a passing cloud.
    def fn(*args, api_key=None, **kwargs):
        raise _client_error(429, "rate limited")

    before = gemini.get_active_model()
    with pytest.raises(genai_errors.ClientError):
        asyncio.run(pti_processor._call_gemini_with_retry(fn))
    assert gemini.get_active_model() == before
