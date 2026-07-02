"""normalize_unit strips the <angle brackets> and stray whitespace seen in
imported unit numbers (pure function — no DB)."""
import pytest

from utils.db import normalize_unit


@pytest.mark.parametrize("raw, expected", [
    ("<1280>", "1280"),
    ("<1304 >", "1304"),
    (" <1256>", "1256"),
    ("  709039  ", "709039"),
    ("1214", "1214"),
    ("<>", ""),
    ("", ""),
    (None, ""),
])
def test_normalize_unit(raw, expected):
    assert normalize_unit(raw) == expected
