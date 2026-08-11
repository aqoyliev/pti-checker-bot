"""Unit tests for multi-key parsing and the Gemini API-key failover.

Pure: no network. The "Gemini call" is a stub that raises a transient error for a
"busy" key so we can assert the failover loop moves to the next key.
"""
import asyncio

import httpx
import pytest

from utils import gemini
from utils import pti_processor as pp


# ---------- get_api_keys parsing ----------

def test_get_api_keys_parses_comma_separated(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEYS", " k1, k2 ,k3,")
    assert gemini.get_api_keys() == ["k1", "k2", "k3"]


def test_get_api_keys_dedupes_and_drops_placeholder_and_blanks(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEYS", f"k1,k1,{gemini._GEMINI_KEY_PLACEHOLDER},, k2")
    assert gemini.get_api_keys() == ["k1", "k2"]


def test_get_api_keys_falls_back_to_single_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEYS", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "solo")
    assert gemini.get_api_keys() == ["solo"]


# ---------- failover ----------

def test_failover_retries_next_key_on_overload(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEYS", "busy,fresh")
    used = []

    def fake_call(*args, api_key=None, **kwargs):
        used.append(api_key)
        if api_key == "busy":
            raise httpx.ConnectError("simulated overload")  # a transient error
        return "ok"

    result = asyncio.run(pp._call_gemini_with_retry(fake_call))
    assert result == "ok"
    assert used == ["busy", "fresh"]  # failed over in order


def test_failover_raises_after_all_keys_exhausted(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEYS", "a,b")

    def always_overloaded(*args, api_key=None, **kwargs):
        raise httpx.ConnectError("down")

    with pytest.raises(httpx.ConnectError):
        asyncio.run(pp._call_gemini_with_retry(always_overloaded))


def test_non_transient_error_does_not_failover(monkeypatch):
    # A bad-request/auth error is not "overload" — it must propagate immediately,
    # not waste the other keys on a request that will fail the same way.
    monkeypatch.setenv("GEMINI_API_KEYS", "k1,k2")
    used = []

    def boom(*args, api_key=None, **kwargs):
        used.append(api_key)
        raise ValueError("bad request")

    with pytest.raises(ValueError):
        asyncio.run(pp._call_gemini_with_retry(boom))
    assert used == ["k1"]  # never tried k2
