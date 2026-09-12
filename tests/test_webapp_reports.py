"""The web panel's report-PDF download route.

Exercises the wiring (window parsing, filename, error translation) with
scripts.fleet_report's DB fetch and Chromium call stubbed out -- neither
belongs in a pure unit test.
"""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from webapp import server


class _Req(dict):
    def __init__(self, match_info=None, query=None):
        super().__init__(admin={"user_id": 1, "is_super_admin": True})
        self.match_info = {k: str(v) for k, v in (match_info or {}).items()}
        self.query = query or {}


def _call(handler, **kwargs):
    resp = asyncio.run(handler(_Req(**kwargs)))
    return resp


EMPTY_DATA = {"groups": [], "drivers": [], "window": [], "alltime": {}}


@pytest.fixture
def stubbed(monkeypatch):
    monkeypatch.setattr(server._report, "fetch", AsyncMock(return_value=EMPTY_DATA))

    def _fake_to_pdf(html_text, out):
        out.write_bytes(b"%PDF-1.4 fake")

    monkeypatch.setattr(server._report, "to_pdf", _fake_to_pdf)


def test_stats_pdf_downloads_with_a_dated_filename(stubbed):
    resp = _call(server.api_report_pdf, match_info={"which": "stats"})

    assert resp.status == 200
    assert resp.content_type == "application/pdf"
    assert resp.body == b"%PDF-1.4 fake"
    cd = resp.headers["Content-Disposition"]
    assert "attachment" in cd and ".pdf" in cd
    assert "driver-report" not in cd


def test_driver_pdf_filename_says_so(stubbed):
    resp = _call(server.api_report_pdf, match_info={"which": "driver"})

    assert "driver-report" in resp.headers["Content-Disposition"]


def test_unknown_report_kind_is_a_clean_404(stubbed):
    resp = _call(server.api_report_pdf, match_info={"which": "nonsense"})

    assert resp.status == 404


def test_unknown_window_is_a_clean_400(stubbed):
    resp = _call(server.api_report_pdf, match_info={"which": "stats"},
                query={"window": "last_decade"})

    assert resp.status == 400


def test_missing_chromium_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(server._report, "fetch", AsyncMock(return_value=EMPTY_DATA))

    def _no_chrome(html_text, out):
        raise SystemExit("No Chromium binary found; cannot render PDF.")

    monkeypatch.setattr(server._report, "to_pdf", _no_chrome)

    resp = _call(server.api_report_pdf, match_info={"which": "stats"})

    assert resp.status == 500
    body = json.loads(resp.body)
    assert "chromium" in body["error"].lower() or "cannot render" in body["error"].lower()
