"""Unit tests for the Mini App initData validation (webapp/auth.py).

Pure: signatures are minted locally with the same dummy BOT_TOKEN the test env
uses — no network, no DB.
"""
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from data.config import BOT_TOKEN
from webapp.auth import MAX_AGE_SECONDS, extract_user, parse_init_data


def make_init_data(
    token: str = BOT_TOKEN,
    user_id: int = 123456789,
    auth_date: int | None = None,
    tamper_hash: bool = False,
) -> str:
    fields = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAF9c2VfdGVzdA",
        "user": json.dumps({"id": user_id, "first_name": "Test"}),
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if tamper_hash:
        fields["hash"] = "0" * 64
    return urlencode(fields)


def test_valid_init_data_parses():
    data = parse_init_data(make_init_data())
    assert data is not None
    assert extract_user(data) == {"id": 123456789, "first_name": "Test"}


def test_bad_signature_rejected():
    assert parse_init_data(make_init_data(tamper_hash=True)) is None


def test_wrong_token_rejected():
    blob = make_init_data(token="999999:some-other-bot-token")
    assert parse_init_data(blob) is None


def test_stale_auth_date_rejected():
    stale = int(time.time()) - MAX_AGE_SECONDS - 60
    assert parse_init_data(make_init_data(auth_date=stale)) is None


def test_fresh_auth_date_accepted_near_boundary():
    almost = int(time.time()) - MAX_AGE_SECONDS + 60
    assert parse_init_data(make_init_data(auth_date=almost)) is not None


def test_empty_and_garbage_rejected():
    assert parse_init_data("") is None
    assert parse_init_data("not a query string") is None
    assert parse_init_data("a=1&b=2") is None  # no hash at all


def test_tampered_field_rejected():
    blob = make_init_data()
    # Swap the signed user id for someone else's without re-signing.
    tampered = blob.replace("123456789", "111111111")
    assert parse_init_data(tampered) is None


def test_extract_user_requires_int_id():
    assert extract_user({"user": json.dumps({"id": "123"})}) is None
    assert extract_user({"user": "not json"}) is None
    assert extract_user({}) is None
