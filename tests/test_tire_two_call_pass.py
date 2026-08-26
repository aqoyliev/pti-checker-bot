"""Regression tests: the tire pass observes in one call and decides in another.

Asked to inspect, the model under-reports. On JRD unit 2456's 2026-08-26 clip -- a
trailer axle worn until the rib grooves were hairlines flush with the tread -- an
inspector-framed pass returned no defect over all 325 frames, over a 41-frame close-up
window, over 19 frames, over 6, and over a 2-frame pair against the deepest tire in the
clip. Asked to DESCRIBE the same frames with no verdict to reach, the same model called
those grooves "almost flush on a flat, smoothed tread face with virtually no open
channel shadow" and ranked them last, every run.

So the image call only observes and a text-only call applies the policy. These tests
pin the three things that made that work, each of which is easy to undo by editing a
prompt back toward the old shape.
"""
from types import SimpleNamespace

from utils import gemini as G


class _FakeModels:
    def __init__(self, sink):
        self.sink = sink

    def generate_content(self, *, model, config, contents):
        self.sink.append(SimpleNamespace(config=config, contents=contents))
        return SimpleNamespace(text='{"tire_defect": false, "issues": []}')


def _fake_genai(sink):
    return SimpleNamespace(Client=lambda **kw: SimpleNamespace(models=_FakeModels(sink)))


SURVEY = {
    "survey": [{"timestamp": "3:17",
                "center_grooves": "faint wavy lines lying almost flush on a flat face",
                "shoulder_note": "shoulder band is flat"}],
    "ranked_most_to_least_depth": ["1:04", "3:17"],
    "tires_fully_shown": False,
}


def test_the_decision_never_gets_the_frames_back(monkeypatch):
    """Re-looking is the step that fails, so the second call is text only.

    One image call (the survey); the decision that follows carries the survey's own
    words and nothing else. If a future edit hands it the frames again, the pass is
    back to the shape that missed the worn axle.
    """
    calls = []
    photo_calls = []

    def fake_photos(images, **kwargs):
        photo_calls.append((images, kwargs))
        return SimpleNamespace(text='{"survey": [], "ranked_most_to_least_depth": []}')

    monkeypatch.setattr(G, "call_gemini_photos", fake_photos)
    monkeypatch.setattr(G, "genai", _fake_genai(calls))

    G.call_gemini_tires([("a.jpg", "image/jpeg", "Video frame at 0:01")], api_key="k")

    assert len(photo_calls) == 1
    assert photo_calls[0][1]["system_prompt"] is G.TIRE_SURVEY_PROMPT
    assert len(calls) == 1
    assert calls[0].config.system_instruction is G.TIRE_DECIDE_PROMPT
    assert all(isinstance(c, str) for c in calls[0].contents), "the decision must be text only"


def test_both_calls_use_the_same_key(monkeypatch):
    """_call_gemini_with_retry fails the pair over together, so they must agree."""
    calls = []
    seen = {}

    def fake_photos(images, **kwargs):
        seen["photos"] = kwargs.get("api_key")
        return SimpleNamespace(text='{"survey": []}')

    monkeypatch.setattr(G, "call_gemini_photos", fake_photos)
    monkeypatch.setattr(G, "genai", SimpleNamespace(
        Client=lambda **kw: (seen.update(decision=kw.get("api_key")),
                             SimpleNamespace(models=_FakeModels(calls)))[1]))

    G.call_gemini_tires([("a.jpg", "image/jpeg", "f")], api_key="key-2")
    assert seen["photos"] == seen["decision"] == "key-2"


def test_the_survey_carries_no_verdict_vocabulary():
    """Wording that invites a verdict is what flipped "flush" back to "open channel".

    The survey describes; it does not report, flag, classify or cite a regulation.
    """
    low = G.TIRE_SURVEY_PROMPT.lower()
    for word in ("report", "flag", "defect", "out-of-service", "oos", "49 cfr"):
        assert word not in low, f"the survey prompt must not ask for a verdict: {word!r}"


def test_the_survey_keeps_the_shoulder_out_of_the_center_reading():
    """One combined field made a healthy tire indistinguishable from a worn one.

    The outermost rib is smoother and shallower by design on every commercial tire, so
    a stack of perfectly good spares came back "shallow and close to flush" -- the same
    words as the genuinely worn axle. Separate fields are what tell them apart, and the
    decision is forbidden from reading the shoulder one.
    """
    assert "center_grooves" in G.TIRE_SURVEY_PROMPT
    assert "shoulder_note" in G.TIRE_SURVEY_PROMPT
    assert "shoulder_note" in G.TIRE_DECIDE_PROMPT
    assert "shoulder" in G.TIRE_DECIDE_PROMPT.lower()


def test_the_decision_still_forces_advisory_not_oos():
    """Company policy: worn tread is an advisory. merge_tire_pass enforces it too, but
    the prompt must not be asking for OOS in the first place."""
    assert '"oos": false' in G.TIRE_DECIDE_PROMPT
