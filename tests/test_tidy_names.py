"""One shape for every stored driver name, whoever typed it.

Before this, the automatic setup stored "Vazquez Lizbeth" while an admin fixing
the group next door stored "SAINTIL, FEDJ", and the fleet report printed both
styles down one column. `tidy_name` is the shape; utils/db applies it on every
write and once over the rows that predate it.
"""
import asyncio

from utils import db
from utils.driver_names import tidy_name


def test_what_the_fleet_types_comes_out_like_the_automatic_setup():
    assert tidy_name("SAINTIL, FEDJ") == "Saintil Fedj"
    assert tidy_name("JEAN JACQUES, WAD COSNER") == "Jean Jacques Wad Cosner"
    assert tidy_name("XIRSI , XIRSI YUUSUF") == "Xirsi Xirsi Yuusuf"
    assert tidy_name("  HYACINTHE   JOHNSON ") == "Hyacinthe Johnson"


def test_a_name_typed_all_in_lower_case_is_cased_too():
    assert tidy_name("vinsky hand") == "Vinsky Hand"


def test_a_deliberately_cased_name_is_left_alone():
    """.title() would turn McDonald into Mcdonald."""
    assert tidy_name("Kevin McDonald") == "Kevin McDonald"
    assert tidy_name("DJ CueVayb") == "DJ CueVayb"
    assert tidy_name("Charles charlson") == "Charles charlson"


def test_hyphens_and_apostrophes_keep_their_capitals():
    assert tidy_name("JEAN-PIERRE O'NEIL") == "Jean-Pierre O'Neil"


def test_digits_and_emoji_survive():
    """A name an admin types is how the driver is known -- tidy_name shapes it,
    it does not judge it. (Parsing the About text is stricter, elsewhere.)"""
    assert tidy_name("Lovensky 509") == "Lovensky 509"
    assert tidy_name("TCHEVO ENTERPRISE LLC🚛") == "Tchevo Enterprise Llc🚛"


def test_it_is_idempotent():
    for raw in ("SAINTIL, FEDJ", "Kevin McDonald", "vinsky hand", "H"):
        assert tidy_name(tidy_name(raw)) == tidy_name(raw)


def test_nothing_in_is_nothing_out():
    assert tidy_name(None) == ""
    assert tidy_name("   ") == ""


# ---------- the backfill ----------

def test_the_backfill_fixes_only_what_is_out_of_shape():
    rows = [{"id": 1, "name": "Vazquez Lizbeth"},
            {"id": 2, "name": "SAINTIL, FEDJ"},
            {"id": 3, "name": "Kevin McDonald"},
            {"id": 4, "name": "JUDE"}]

    assert db.untidy_driver_names(rows) == [(2, "Saintil Fedj"), (4, "Jude")]


def test_the_backfill_leaves_a_blank_name_for_a_person():
    rows = [{"id": 1, "name": None}, {"id": 2, "name": ""}, {"id": 3, "name": "   "}]

    assert db.untidy_driver_names(rows) == []


def test_a_second_pass_finds_nothing():
    rows = [{"id": 2, "name": "SAINTIL, FEDJ"}]
    fixed = [{"id": i, "name": n} for i, n in db.untidy_driver_names(rows)]

    assert db.untidy_driver_names(fixed) == []


# ---------- every write goes through it ----------

class _Conn:
    """Just enough of an asyncpg connection to see what gets written."""

    def __init__(self):
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append(args)
        return "DELETE 1"

    async def fetch(self, sql, *args):
        self.calls.append(args)
        return []

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self):
        self.conn = _Conn()

    def acquire(self):
        return self.conn

    async def execute(self, sql, *args):
        self.conn.calls.append(args)


def _written_names(monkeypatch, call):
    pool = _Pool()
    monkeypatch.setattr(db, "_pool", pool)
    asyncio.run(call)
    return [a[2] for a in pool.conn.calls if len(a) >= 3 and isinstance(a[2], str)]


def test_adding_a_driver_stores_the_tidy_name(monkeypatch):
    assert _written_names(monkeypatch, db.add_driver(-1, 5, "SAINTIL, FEDJ")) == [
        "Saintil Fedj"]


def test_replacing_drivers_stores_tidy_names(monkeypatch):
    call = db.replace_drivers(-1, [{"user_id": 5, "name": "GONZALEZ OSVALDO"}])
    assert _written_names(monkeypatch, call) == ["Gonzalez Osvaldo"]


def test_a_swap_stores_the_tidy_name(monkeypatch):
    assert _written_names(monkeypatch, db.swap_driver(-1, 5, 6, "CIUS, SAUREL")) == [
        "Cius Saurel"]


def test_a_rename_compares_and_stores_the_tidy_name(monkeypatch):
    """Renaming "Saintil Fedj" to "SAINTIL, FEDJ" must read as the no-op it is."""
    call = db.set_driver_names([(-1, 5, "SAINTIL, FEDJ")])
    assert _written_names(monkeypatch, call) == ["Saintil Fedj"]
