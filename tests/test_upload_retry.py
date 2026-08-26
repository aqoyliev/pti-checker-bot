"""A dropped upload session is a transit failure, not a rejected file.

Gemini reports a resumable upload that died mid-transfer as a *400* --
``{'message': 'Upload has already been terminated.'}`` -- which every other
part of the code reads as "permanently refused". On 2026-08-25 that sank a
445-frame inspection on the first attempt and printed the raw API JSON into the
driver's group.
"""
from __future__ import annotations

import pytest
from google.genai import errors as genai_errors

from utils.gemini import _UPLOAD_ATTEMPTS, _upload_one, is_retryable_upload_error
import utils.pti_processor as pti_processor


def _client_error(code: int, message: str) -> genai_errors.ClientError:
    return genai_errors.ClientError(code, {"message": message, "status": "Bad Request"}, None)


TERMINATED = "Upload has already been terminated."


def test_terminated_upload_is_retryable():
    assert is_retryable_upload_error(_client_error(400, TERMINATED))


def test_quota_and_server_errors_are_retryable():
    assert is_retryable_upload_error(_client_error(429, "quota exceeded"))
    assert is_retryable_upload_error(
        genai_errors.ServerError(503, {"message": "overloaded"}, None))


@pytest.mark.parametrize("code,message", [
    (400, "Unsupported mime type: application/zip"),
    (403, "Permission denied"),
])
def test_a_refused_file_is_not_retried(code, message):
    """Retrying these just costs the driver three times the wait."""
    assert not is_retryable_upload_error(_client_error(code, message))


class _Client:
    """Fails `fails` times with `exc`, then succeeds."""
    def __init__(self, exc, fails):
        self.exc, self.fails, self.calls = exc, fails, 0
        self.files = self

    def upload(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fails:
            raise self.exc
        return "uploaded"


def test_upload_retries_a_dropped_session(monkeypatch):
    monkeypatch.setattr("utils.gemini.time.sleep", lambda s: None)
    client = _Client(_client_error(400, TERMINATED), fails=1)
    assert _upload_one(client, "/tmp/f.jpg", "image/jpeg", "Frame") == "uploaded"
    assert client.calls == 2


def test_upload_gives_up_after_the_attempt_budget(monkeypatch):
    monkeypatch.setattr("utils.gemini.time.sleep", lambda s: None)
    client = _Client(_client_error(400, TERMINATED), fails=99)
    with pytest.raises(genai_errors.ClientError):
        _upload_one(client, "/tmp/f.jpg", "image/jpeg", "Frame")
    assert client.calls == _UPLOAD_ATTEMPTS


def test_a_refused_file_fails_on_the_first_attempt(monkeypatch):
    monkeypatch.setattr("utils.gemini.time.sleep", lambda s: None)
    client = _Client(_client_error(400, "Unsupported mime type"), fails=99)
    with pytest.raises(genai_errors.ClientError):
        _upload_one(client, "/tmp/f.jpg", "image/jpeg", "Frame")
    assert client.calls == 1


def test_processor_treats_it_as_transient():
    """So it fails over to the next API key and shows the try-again message
    instead of dumping the raw error into the group."""
    assert pti_processor._is_transient(_client_error(400, TERMINATED))
    assert not pti_processor._is_transient(_client_error(400, "Unsupported mime type"))
