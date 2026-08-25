"""Unit tests for the runtime-settable Gemini model (admin model switching).

Pure: no DB / no network. Each test restores the active model so ordering and
other suites are unaffected.

These deliberately avoid hardcoding a model id beyond the registry itself: on
2026-08-20 every ``gemini-2.5-*`` id was retired by Google at once, and tests
that named one directly failed for a reason that had nothing to do with the
behaviour they were checking.
"""
from utils import gemini


def _restore(original):
    gemini.set_active_model(original)


def _other_model() -> str:
    """Any valid model that isn't the default — the thing to switch *to*."""
    return next(m for m in gemini.AVAILABLE_GEMINI_MODELS
                if m != gemini.DEFAULT_GEMINI_MODEL)


def test_default_model_is_first_available():
    assert gemini.DEFAULT_GEMINI_MODEL == gemini.AVAILABLE_GEMINI_MODELS[0]


def test_there_is_more_than_one_choice():
    # The admin panel offers a switch, and model failover needs somewhere to go.
    assert len(gemini.AVAILABLE_GEMINI_MODELS) > 1


def test_no_model_without_a_working_key_is_offered():
    # gemini-3-pro-preview 404s on every production key, so selecting it could
    # only fail. 2.5-pro is different: it 404s on the newer keys but still works
    # on the grandfathered one, and _call_gemini_with_retry only abandons a model
    # once every key has refused it. See utils/gemini.py's note on 2026-08-20.
    assert "gemini-3-pro-preview" not in gemini.AVAILABLE_GEMINI_MODELS
    assert "gemini-2.5-flash-lite" not in gemini.AVAILABLE_GEMINI_MODELS
    # And no flash-lite at all: it is the cheapest *and* the least accurate,
    # so leaving it in the failover tail risks a silent accuracy drop.
    assert not [m for m in gemini.AVAILABLE_GEMINI_MODELS if "lite" in m]


def test_a_fallback_sits_after_the_default():
    # If a key ever loses access to the default the bot has to land somewhere,
    # so the default must not be the only entry.
    assert gemini.AVAILABLE_GEMINI_MODELS[1:] != ()


# Input $/1M, which is ~98% of a PTI's bill: ~150 frames at ~1100 tokens each,
# sent twice, against a ~1.5k-token verdict. Aliases are absent on purpose --
# they resolve to a different model over time, so no fixed price can be asserted.
_INPUT_PRICE_PER_1M = {
    "gemini-3.7-flash": 0.75,
    "gemini-3.6-flash": 0.75,
    "gemini-3.5-flash": 1.50,
    "gemini-3.1-pro-preview": 2.00,
    "gemini-2.5-pro": 1.25,
}


def test_failover_never_escalates_the_bill():
    """Falling back must get cheaper, never dearer.

    The list used to read 2.5-pro, then 3.1-pro. On the Gurman keys 2.5-pro 404s
    on *every* key, so the first fallback was the single most expensive model on
    the menu -- ~$0.70 an inspection against the ~$0.26 intended, charged
    silently with nothing in the chat to say the model had moved. A failover
    fires exactly when nobody is watching, so the ordering is the only guard.
    """
    priced = [(m, _INPUT_PRICE_PER_1M[m]) for m in gemini.AVAILABLE_GEMINI_MODELS
              if m in _INPUT_PRICE_PER_1M]
    # 2.5-pro is the one deliberate exception: it is dead on the newer keys, so
    # it sits low as a JRD-only fallback rather than at its price.
    priced = [(m, p) for m, p in priced if m != "gemini-2.5-pro"]
    for (cheap, cheap_price), (dear, dear_price) in zip(priced, priced[1:]):
        assert cheap_price <= dear_price, (
            f"{cheap} (${cheap_price}/1M) falls back to {dear} "
            f"(${dear_price}/1M) -- failover must not cost more")


def test_every_offered_model_has_a_priced_hint():
    # An admin switching models from either panel is making a spend decision;
    # an unlabelled id in the list is one they'd be making blind.
    for model in gemini.AVAILABLE_GEMINI_MODELS:
        assert gemini.MODEL_HINTS.get(model), f"{model} has no admin hint"


def test_set_active_model_switches_when_valid():
    original = gemini.get_active_model()
    target = _other_model()
    try:
        assert gemini.set_active_model(target) is True
        assert gemini.get_active_model() == target
    finally:
        _restore(original)


def test_set_active_model_rejects_unknown_and_keeps_current():
    original = gemini.get_active_model()
    target = _other_model()
    try:
        gemini.set_active_model(target)
        assert gemini.set_active_model("not-a-real-model") is False
        # Unchanged from the last valid value, not silently broken.
        assert gemini.get_active_model() == target
    finally:
        _restore(original)
