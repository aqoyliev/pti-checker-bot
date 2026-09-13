"""A PTI never decides what vehicle it was filmed on.

Reading the unit number, the plate or the trailer number off the footage and
storing it was removed on 2026-09-13 at the fleet's instruction. A stencilled
number on a dirty panel is the least legible thing in a walkaround, and
everything it could have been written to already has a better source: the
truck's unit comes from the chat title and is swept daily, the drivers come
from the roster.

The model is still *asked* for a `vehicles` block — the prompt is not ours to
edit — so the thing to guard is that nothing acts on the answer. Three ways it
could creep back: a helper that reads it, a merge that carries it, or a write
that stores it.

Pure: no network, no database.
"""
import pathlib

from handlers.groups import pti
from utils import db, pti_processor

REPO = pathlib.Path(__file__).resolve().parent.parent


def _source(module) -> str:
    return pathlib.Path(module.__file__).read_text(encoding="utf-8")


# ---------- nothing reads the model's vehicle block ----------

def test_the_pti_handler_has_no_vehicle_readers():
    source = _source(pti)
    for gone in ("_extract_vehicles", "truck_verdict", "_reconcile_vehicles",
                 "_truck_log_fields", "_report_truck_change"):
        assert gone not in source, f"{gone} is back in handlers/groups/pti.py"


def test_the_frame_merge_does_not_carry_vehicles():
    # merge_frame_passes rebuilds the result from an explicit list of keys, so
    # leaving "vehicles" out of it is what keeps the reading from travelling.
    merged = pti_processor.merge_frame_passes([
        {"checked_clean": [], "issues": [], "missing_areas": [],
         "vehicles": [{"type": "truck", "unit_number": "2570", "plate": "ABC123"}]},
    ])
    assert "vehicles" not in merged


# ---------- nothing writes a vehicle identity ----------

def test_no_db_helper_stores_a_vehicle_reading():
    source = _source(db)
    for gone in ("def set_trailer", "def set_truck_plate", "def set_truck_unit"):
        assert gone not in source, f"{gone} is back in utils/db.py"


def test_a_pti_is_never_a_source_for_the_unit():
    # set_group_unit is the panel's write and belongs to an admin; the title
    # sweep has its own. Neither may be reachable from the inspection path.
    source = _source(pti)
    assert "set_group_unit" not in source
    assert "set_group_title" not in source


def test_the_pti_log_records_no_vehicle_at_all():
    # Neither the plate nor the unit: log_pti has no parameter for either, so
    # a future caller cannot re-introduce a reading by passing one. Leaving
    # pti_log.unit_number empty is also what keeps the previous-inspection
    # history switched off for the model — the fleet's decision on 2026-09-13,
    # since the results it is happy with are the results with no history.
    import inspect
    params = inspect.signature(db.log_pti).parameters
    assert "plate" not in params
    assert "unit_number" not in params


# ---------- the panel offers no field nothing maintains ----------

def test_the_panel_shows_no_plate_or_trailer():
    for name in ("webapp/server.py", "webapp/static/index.html"):
        source = (REPO / name).read_text(encoding="utf-8")
        for gone in ("truck_plate", "trailer_unit", "trailer_plate"):
            assert gone not in source, f"{gone} is back in {name}"
