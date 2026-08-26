from __future__ import annotations

import glob
import sys
import os
import json
import logging
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpcore
import httpx
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

load_dotenv()

PTI_FRAME_INTERVAL = float(os.getenv("PTI_FRAME_INTERVAL", "1"))
MAX_VIDEO_DURATION = 900   # 15 minutes — reject anything longer
MAX_FRAMES = 900           # safety cap so we never exceed Gemini's token limit
FILE_API_THRESHOLD = 50    # frames above this count are uploaded via File API instead of sent inline

# The vision model used for every PTI pass. Admins switch this at runtime from the
# /admin panel (set_active_model), and the choice is persisted in the DB and
# reloaded on startup. Keep DEFAULT first so a blank/unknown stored value falls
# back to it; the rest of the tuple is the failover order.
#
# 2026-08-20: every ``gemini-2.5-*`` id started returning **404 NOT_FOUND** --
# "no longer available to new users" -- and PTI checking stopped dead for ~2 days.
# Note the failure mode: that is a *404*, not the 429/503 the retry path treats as
# transient, so nothing failed over and every inspection simply errored out.
#
# **The 404 is per-account, not global.** Google grandfathers older AI Studio
# projects: a key issued before the cutoff still serves 2.5, a newer one does not.
# The two fleets landed on opposite sides of that line -- JRD's project (created
# months earlier) still serves 2.5-pro, Gurman's (created 2026-08-21) 404s the
# whole 2.5 family, pro *and* flash *and* flash-lite. So the default cannot be a
# 2.5 id: on Gurman every key 404s it, the failover walks to the next entry, and
# whatever sits there is what the fleet actually pays for.
#
# **Cost is why the order is what it is.** A PTI is a vision workload -- ~150
# frames at ~1100 tokens each, sent twice (broad pass + tire pass), against a
# verdict of ~1.5k output tokens. Input price is therefore ~98% of the bill and
# output price is nearly irrelevant, which inverts the usual ranking. Measured per
# inspection on 2026-08-25 prices:
#
#     gemini-3.7-flash        $0.75/$3.75      ~$0.26   <- default
#     gemini-3.6-flash        $0.75/$3.75      ~$0.26
#     gemini-2.5-pro          $1.25/$10.00     ~$0.44   (grandfathered keys only)
#     gemini-3.5-flash        $1.50/$9.00      ~$0.53
#     gemini-3.1-pro-preview  $2.00/$12.00     ~$0.70
#
# The old list had 2.5-pro first and **3.1-pro second**, so Gurman's 404 walked it
# straight onto the single most expensive model on the menu -- 2.7x the intended
# spend, silently, with nothing in the chat to say so. Order failover so it
# degrades in cost, never escalates: if you add a model, price it first and put it
# where that price belongs.
#
# 3.7-flash's $0.75 input is a promotional rate **through 2026-12-31**; it doubles
# to $1.50 on 2027-01-01, at which point re-check this table rather than assuming
# the ordering still holds.
#
# ``gemini-2.5-pro`` stays selectable for JRD, whose keys still serve it, but it is
# no longer the default because it is dead on half the estate. The two alias ids
# are the tail of the failover chain on purpose: they always resolve to something
# live, which is what you want once every pinned id has failed, but they can move
# under you without notice so nothing should *start* on one. Every entry below was
# verified callable on all four Gurman production keys on 2026-08-25.
#
# No flash-lite id is offered, though at ~$0.11 an inspection one would be the
# cheapest thing here by far. Cheapest and weakest are the same model, and this
# list is also the failover chain -- so including it means an unattended
# fallback can put the least accurate model behind verdicts that reach drivers
# as authoritative. The tire pass and the ABS-lamp rules exist because this
# prompt is sensitive to exactly that. Trading accuracy for cost is a decision
# to take deliberately, not one to leave lying in a failover tail.
DEFAULT_GEMINI_MODEL = "gemini-3.7-flash"
AVAILABLE_GEMINI_MODELS = (
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.1-pro-preview",
    "gemini-2.5-pro",
    "gemini-flash-latest",
    "gemini-pro-latest",
)
# Admin-facing label for each id, shown by both the inline panel and the web
# panel. It carries the **price** because picking a model here is a spend
# decision -- ~$0.26 vs ~$0.70 per inspection is the difference between the top
# and bottom of this list, and an admin choosing blind has no way to see that.
# Lives here rather than in either panel so the two can't drift apart, and so it
# sits next to the ordering rationale above that it has to stay consistent with.
MODEL_HINTS = {
    "gemini-3.7-flash": "$0.75/1M in — ~$0.26 a PTI (promo until 2027)",
    "gemini-3.6-flash": "$0.75/1M in — ~$0.26 a PTI (promo until 2027)",
    "gemini-3.5-flash": "$1.50/1M in — ~$0.53 a PTI",
    "gemini-3.1-pro-preview": "$2.00/1M in — ~$0.70 a PTI, the dearest here",
    "gemini-2.5-pro": "$1.25/1M in — ~$0.44 a PTI; 404s on newer keys",
    "gemini-flash-latest": "alias — whatever Flash is current, price varies",
    "gemini-pro-latest": "alias — whatever Pro is current, price varies",
}


_active_model = DEFAULT_GEMINI_MODEL


def get_active_model() -> str:
    """The Gemini model id currently used for inspections."""
    return _active_model


def set_active_model(model: str) -> bool:
    """Switch the active model. Returns False (and changes nothing) if `model` is
    not one of AVAILABLE_GEMINI_MODELS, so a bad value can't silently break calls."""
    global _active_model
    if model not in AVAILABLE_GEMINI_MODELS:
        return False
    _active_model = model
    return True


_GEMINI_KEY_PLACEHOLDER = "your-gemini-key-here"


def get_api_keys() -> list[str]:
    """All usable Gemini API keys, in priority order.

    Reads ``GEMINI_API_KEYS`` (comma-separated) first, falling back to the single
    ``GEMINI_API_KEY``. Blanks, the placeholder, and duplicates are dropped. Callers
    (utils.pti_processor._call_gemini_with_retry) try keys in order and fail over to
    the next one on a 503/429/transient error, so a busy key doesn't sink the call."""
    raw = os.getenv("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEY") or ""
    seen: set[str] = set()
    keys: list[str] = []
    for k in raw.split(","):
        k = k.strip()
        if k and k != _GEMINI_KEY_PLACEHOLDER and k not in seen:
            seen.add(k)
            keys.append(k)
    return keys


def _resolve_api_key(api_key: str | None) -> str:
    """The key to use for one call: an explicit `api_key` (from the failover loop),
    else the first configured key. Raises if none is set."""
    if api_key:
        return api_key
    keys = get_api_keys()
    if not keys:
        raise ValueError("No Gemini API key set (GEMINI_API_KEYS or GEMINI_API_KEY)")
    return keys[0]


class VideoTooLongError(Exception):
    def __init__(self, duration: float):
        self.duration = duration
        super().__init__(f"Video duration {duration:.0f}s exceeds the {MAX_VIDEO_DURATION}s limit")


# A resumable upload session that dies mid-transfer comes back as a *400*, not a
# 5xx: ``400 Bad Request {'message': 'Upload has already been terminated.'}``.
# Nothing about the file is wrong -- a fresh session almost always takes it -- but
# a 400 reads as "permanently rejected" everywhere else, so it has to be named
# explicitly. Seen live on 2026-08-25, where it sank a whole 445-frame inspection
# and printed the raw API error into the driver's group.
_UPLOAD_ATTEMPTS = 3
_UPLOAD_BACKOFF = 1.0


def is_retryable_upload_error(e: BaseException) -> bool:
    """True for an upload that failed *in transit* rather than being refused."""
    if isinstance(e, genai_errors.ServerError):
        return True
    if isinstance(e, genai_errors.ClientError):
        if getattr(e, "code", None) == 429:
            return True
        text = str(e).lower()
        return "upload" in text and ("terminated" in text or "aborted" in text)
    return isinstance(e, (httpx.HTTPError, httpcore.NetworkError, OSError))


def _upload_one(client, path: str, mime_type: str, label: str):
    """Upload one frame, retrying a torn-down upload session.

    Frames go up 8 at a time and a long PTI is hundreds of them, so a single
    dropped session must not sink the inspection. Only transit failures are
    retried; a genuinely rejected file (bad mime, auth) still fails on the first
    attempt.
    """
    for attempt in range(1, _UPLOAD_ATTEMPTS + 1):
        try:
            return client.files.upload(
                file=path,
                config=genai_types.UploadFileConfig(mime_type=mime_type, display_name=label[:40]),
            )
        except Exception as e:
            if attempt == _UPLOAD_ATTEMPTS or not is_retryable_upload_error(e):
                raise
            logging.warning(
                f"Upload of {label!r} failed ({type(e).__name__}), attempt "
                f"{attempt}/{_UPLOAD_ATTEMPTS}; retrying"
            )
            time.sleep(_UPLOAD_BACKOFF * attempt)


def _delete_files_background(client, files: list):
    """Delete uploaded files in parallel without blocking the caller."""
    def _del(uf):
        try:
            client.files.delete(name=uf.name)
        except Exception:
            logging.warning(f"Failed to delete uploaded file {uf.name}", exc_info=True)

    pool = ThreadPoolExecutor(max_workers=16)
    for uf in files:
        if uf is not None:
            pool.submit(_del, uf)
    pool.shutdown(wait=False)

SYSTEM_PROMPT = """You are an experienced commercial-truck inspector. \
Analyze the supplied frames/photos from a pre-trip inspection (PTI) of a semi-truck and/or trailer as one combined inspection.

Inspection scope — check ONLY these areas. Do not flag anything outside this list.
The first 8 are REQUIRED (every one must be filmed); "Under the hood" is OPTIONAL (inspect and report it if it was filmed, but NOT filming it never fails the inspection — see the completeness rule):
  1. Brake pads — truck and trailer (look for severely worn pads, missing pads, broken hardware)
  2. Lights — truck and trailer (headlights, marker, turn, brake, tail). Every light group must be shown actually WORKING (illuminated/activated), including the trailer REAR — see the lights-completeness rule below. Flag a light only when clearly broken / missing / non-functional.
  3. Tires — tread depth and air pressure (truck and trailer). EVERY tire position must be filmed — see the tire-completeness rule below.
  4. Side mirrors — 4 total, 2 on the hood. Check BOTH the outer housing AND the inner reflective glass face of each mirror (damage can be on the glass only). Flag only clearly broken / missing / cracked.
  5. Windshield (flag only clear cracks/chips in the driver's view)
  6. Air lines & cables — flag visible cuts, leaks, or disconnected lines; ALSO flag air/electrical lines that rest on or chafe against the catwalk or frame (an advisory to secure them — see the air-line rule below).
  7. Overall frame / chassis (flag only clearly cracked, bent, or damaged frame members)
  8. ABS malfunction lamp (trailer) — the warning indicator next to a stamped "ABS" label. It MUST be filmed (it is normally OFF). See the ABS rule below for how to judge on/off and where to place it.
  9. Under the hood — engine oil level and visible leaks. OPTIONAL: if it was filmed, inspect it and report any leak (and add "Under hood" to "checked_clean" if it looks fine); if it was NOT filmed, do NOT list it in "missing_areas" and do NOT fail the inspection over it.

Fire extinguisher and warning triangle (presence) — track this SEPARATELY from the 9 required areas above; it does NOT affect completeness or PASS/FAIL. Set "fire_extinguisher_shown": true if that storage area appears in any frame (regardless of whether the extinguisher/triangle itself is present), or false if it was never filmed at all. If the area IS visible and you can clearly confirm the extinguisher or triangle is actually missing, you may still report that as an advisory issue (oos=false, see the NEVER OOS list below) — that is independent of "fire_extinguisher_shown".

Leniency rules — BE CONSERVATIVE. The default is PASS. It is FAR worse to falsely flag a good component than to miss a borderline defect. Drivers lose trust in this bot when it invents issues. When in doubt: PASS. Never INVENT a defect you cannot describe precisely (size, shape, location, what makes you certain).

  - **Tires — be strict about false positives (shoulder anatomy, mixed patterns) but do NOT miss a clearly smooth/worn center tread.**
    - **Tread depth / wear — observations only, never conclusions.** You CANNOT measure tread depth from a video or photo, so NEVER quote a number ("below 4/32", "2/32 left") or invoke "wear bars". ALSO BANNED — these are conclusions, not observations, and you must NEVER use them: "severe wear", "heavy wear", "tires show wear", "worn tires", "replace soon", "tread is low", or any similar vague summary.
      **The outer SHOULDER of a tire — the curved edge where the tread meets the sidewall — is naturally smoother and less aggressively treaded than the center tread by design.** That is normal tire anatomy on every commercial tire and NEVER a defect. Do NOT flag "shoulder is smooth", "shoulder bald", or "outer edge worn" — those are not real issues. Comparing the center of a tire to its own shoulder is invalid because the two areas are SUPPOSED to look different.
      You MAY flag wear when the CENTER tread (not the shoulder) of ONE dual tire is clearly smoother or more worn than the ADJACENT dual visible in the same frame — whether the worn area is a localized bald patch OR the entire center tread face is smooth/featureless (grooves absent or barely visible). A tire whose whole center tread is smooth/featureless while its adjacent dual still shows clear grooves is an obvious defect and MUST be flagged. Your issue text MUST include both (a) which dual (inner vs outer) and (b) the contrast specifically against the OTHER dual visible in the same frame. Same-tire comparisons (center vs shoulder, left vs right side of one tire) do NOT count as evidence. When in doubt on borderline cases, PASS.
      **Mixed tread patterns are NOT a defect.** Trucks often run two different tire models on the same dual axle (e.g., a block-pattern tread next to a lug-pattern tread) because one tire was replaced and the other wasn't. Different brand, different tread design, different wear age — all NORMAL. Do NOT flag tires just because the two duals look different from each other. The contrast that matters for wear is "smooth/featureless tread on one vs visible grooves on the other", NOT "different shaped tread blocks".
      **Rib-pattern trailer/steer tires are SHALLOW by design — do NOT read a normal rib tire as "worn smooth."** Highway trailer and steer tires use a shallow circumferential RIB tread (a few straight grooves running around the tire) instead of the deep, blocky lugs of a drive tire. A trailer tire showing several continuous circumferential grooves — even if the ribs look low or the face looks fairly smooth between them — is a NORMAL rib tire, NOT worn out. Only call a tire worn-smooth when those circumferential grooves are actually GONE (the tread face is a flat, groove-free band), not merely shallow. **AND the smooth tire must stand out against a grooved neighbour:** if EVERY visible trailer tire looks equally smooth/shallow, that is a uniform rib-pattern set, NOT evidence of wear — there is no grooved adjacent dual to contrast against, so do NOT flag. Never flag "all tires worn"; wear is ONE dual missing its grooves while an adjacent dual in the same frame still shows clear grooves.
      **WET tires read as FALSELY bald — raise the bar in the rain.** A wet, rain-soaked, or freshly-washed tire looks shiny and its grooves fill with water, so the tread looks smoother and balder than it really is. If the frame shows water droplets, road spray, a wet sheen, or it is visibly raining, do NOT flag tread wear unless the grooves are UNMISTAKABLY gone on a clear, head-on, in-focus view — when the tire is wet, default to PASS.
      Valid example: "(1:15) Inner dual center tread smooth/worn vs grooved outer dual (49 CFR 393.75)".
      INVALID — do NOT produce these: "Drive tires show severe visible wear", "Tire heavily worn", "Tread is low", "Outer shoulder is smooth/bald", "Shoulder bald".
    - "Cracked sidewall" requires a visible split in the rubber where you can plainly see a gap or separation in the surface. Weathered-looking rubber, normal sidewall lettering/DOT codes, brand markings, mold lines, shadows, dirt, mud, or texture variation are NOT cracks. If you have to squint, it is not a crack.
    - "Cut sidewall" requires a visible incision — a clean line where the rubber is severed. Scuff marks, paint, mud, shadows, stickers, and brand stamps are NOT cuts.
    - "Bulge" requires a clearly protruding section, not normal sidewall curvature.
    - **Puncture / hole / embedded object — this is the highest false-positive risk area, treat it with extra care.** Do NOT flag a puncture unless ALL of these are true:
        (1) the dark spot is on the FACE of a tread block (the raised rubber pad), NOT inside a groove between blocks;
        (2) you can clearly see it is recessed INTO the rubber (a cavity), not something sitting on top of the rubber;
        (3) it is darker than the surrounding rubber — pebbles, stones, and road debris look LIGHT (white / grey / tan) and reflective, and are NEVER punctures even when they look embedded;
        (4) it is not part of a splash, mud spot, water droplet, paint, road grime, or wet-surface texture pattern.
      If the tire is wet, freshly washed, or has visible mud/grime, raise the bar further — most "dark spots" on a wet tread block are water or mud, not punctures. When in doubt, PASS.
      If you DO flag, your issue text MUST describe (a) which tread block (e.g., "rightmost outer block of the inner dual"), (b) the cavity's apparent shape and depth, and (c) what made you certain it is not a pebble or mud spot. Format: "(M:SS) Puncture in trailer tire tread (49 CFR 393.75)".
    - A tire is FINE unless you can clearly see one of: visibly flat (deformed against the ground), exposed cord/belt fabric, an unmistakable sidewall split or incision, a clear bulge, a puncture/hole/embedded object in the tread or sidewall, or clearly visible bald/smooth patches where the tread pattern is missing or severely faded compared to the rest of the tire or the adjacent dual. Otherwise treat it as PASS — but only add "Tires" to "checked_clean" if EVERY tire position was shown (see tire-completeness below).
    - TIRE COMPLETENESS — film every tire, but a FEW un-shown positions is PASS-with-a-note, NOT a full failure:
        • ALL positions shown (both steer tires, all drive tires — both sides of each drive axle incl. inner and outer duals — and all trailer tires, both sides incl. inner and outer duals): add "Tires" to "checked_clean" and report any defect normally.
        • MOST shown but only ONE or a FEW specific positions not captured (e.g. just the left steer tire, or one inner dual): STILL add "Tires" to "checked_clean" (the area counts as inspected) and do NOT put "Tires" in "missing_areas". Instead name each un-filmed position in "what_was_not_visible" so the driver knows exactly which tire to re-shoot — e.g. "Left steer tire", "Driver-side trailer inner dual". Always specify the axle/side/position; never a bare "Tires".
        • Tires LARGELY not filmed (only one side of the vehicle shown, trailer tires skipped entirely, or essentially no wheel positions visible): put "Tires" in "missing_areas" (INCOMPLETE → FAIL) so the driver re-films the tires.
      In short: a couple of missed positions → "checked_clean" + a specific note saying which tire; wholesale-missing tires → "missing_areas".
  - **Wheels / rims — report a GENUINE structural defect OR severe corrosion; ignore minor cosmetic rust.** Report a wheel/rim as a violation (always oos=false — advisory, see the NEVER OOS list) when you clearly see EITHER:
      (a) a STRUCTURAL defect — a crack line through the metal, a broken or bent/deformed section, elongated or cracked stud (bolt) holes, or a clearly missing or loose lug nut. Cite 49 CFR 393.205. Format: "(M:SS) Cracked trailer rim (49 CFR 393.205(a))".
      (b) SEVERE, WIDESPREAD corrosion — heavy rust with flaking/peeling paint AND visible scaling or pitting across much of the wheel face, well beyond a few rust spots. This is a maintenance/condition advisory, NOT a 393.205 violation — cite 49 CFR 396.3 or omit the CFR; NEVER cite 393.205 for rust alone. Format: "(M:SS) Severe rim corrosion (49 CFR 396.3)".
    Do NOT flag light/minor surface rust, dirt, mud, road grime, brake dust, paint chips, scuffs, stickers, or discoloration — those are normal and NOT defects. When in doubt between "light rust" and "severe corrosion", treat it as light and PASS. Your "evidence" MUST state the extent and location of what you actually saw (e.g. "rust and flaking paint cover most of the wheel face, with pitting around the bolt circle").
  - Air bags / air suspension bellows ("balloons"): these are NOT in scope. Do not flag them. A bag that looks intact is fine.
  - Mud flaps / splash guards: NOT in scope. Do not flag a mud flap as damaged, torn, curled, trimmed, or missing — a flap that is bent, curled at the bottom, flexed, or wet is normal and is not part of this inspection.
  - **Air / electrical lines — secured routing (CHECK THE CATWALK DELIBERATELY).** Besides cuts/leaks/disconnects (those are OOS), a line that is UNSECURED and resting on / draped across / chafing against the catwalk (the flat diamond-plate step deck behind the cab) or the frame rail is a securing violation — flag it as an ADVISORY (oos=false) and tell the driver to secure / re-route it. **In particular: if any air hose or electrical cable is lying ACROSS or sitting ON TOP OF the catwalk deck surface, or hanging draped over the frame rail, that is NOT acceptable routing — you MUST report it.** Properly secured lines run along the back of the cab in their clips/looms and do NOT lie across the catwalk deck; a hose lying on the walking surface will chafe and is a trip hazard. Evidence MUST name which line (air hose vs electrical cable) and where it rests on the catwalk/frame. Do NOT flag lines merely passing NEAR the metal in their normal routing or held in their clips/looms. Format: "(M:SS) Air line resting on catwalk, not secured (49 CFR 393.45)".
  - Lights: only flag a light as a DEFECT if it is obviously broken (visible shattered lens) or missing from its housing. A lamp that is simply unlit is NOT proof it is broken — it may just not have been activated, so do not flag it broken; but an un-demonstrated lamp also does NOT count as "working" for completeness (see the lights-completeness rule next). Don't flag dirt or reflections.
  - **LIGHTS COMPLETENESS — lights must be shown WORKING, not just present.** Add "Lights" to "checked_clean" ONLY when the footage actually shows the lamps ILLUMINATED / ACTIVATED — at minimum the brake (stop) lamps and turn signals operating, plus marker/clearance/tail lamps lit — on BOTH the truck AND the trailer, INCLUDING the trailer REAR lamps. Merely seeing an unlit lamp lens does NOT count as verified. If the lights are visible but never shown working, OR the trailer rear is never filmed at all, do NOT put "Lights" in "checked_clean" — put "Lights" in "missing_areas" so the inspection is INCOMPLETE and FAILs, and the driver re-films with the lights demonstrated working (press the brake / hit the signals, or show the lamps lit). For Lights ONLY, un-demonstrated function counts as incomplete — this is the one exception to the "filmed in one frame = not missing" rule below.
  - **ABS malfunction lamp — this is a WARNING INDICATOR, so the logic is INVERTED from exterior lamps: here illuminated = DEFECT, off = good.** Trailers carry an external ABS malfunction lamp, identified by a stamped/painted "ABS" label right next to it. This lamp is OFF almost all the time — treat OFF as the strong default and only flag it ON with high certainty. The ABS lamp is a SMALL DEDICATED indicator lens that is AMBER / YELLOW (never red) mounted DIRECTLY beside the "ABS" text — touching it or within roughly one lens-width. It is NOT part of the rear marker/clearance/brake/tail lamp cluster lower on the sill or anywhere else on the corner. **A glowing RED lamp is NEVER the ABS lamp** — red lamps are marker, clearance, stop (brake), or tail lamps. The ABS malfunction indicator only ever glows amber/yellow, so if the lit lamp you are looking at is red, it is NOT the ABS lamp and you must NOT flag ABS, no matter how close it sits to the "ABS" stamp. Flag it ONLY when ALL of these hold: (a) a legible "ABS" text label is clearly visible; (b) THE SPECIFIC small lens touching that "ABS" label — not any other lamp — is genuinely SELF-ILLUMINATED, i.e. a bright glow/bloom or halo on THAT lens clearly brighter than its surroundings that washes out its lens detail; AND (c) it reads as lit in at least TWO separate frames (a one-frame flash is a reflection, not a warning lamp). A merely colored, red, amber, dark, or glossy lens is OFF — warning lenses look colored even when unlit, so color is NOT evidence of illumination. A reflection, sun glint, headlight/flash bounce, wet-surface sparkle, or a single bright pixel is NOT illumination. If you cannot read an "ABS" label, do NOT flag — a lit marker lamp, brake lamp, red reflector, or glint is NOT an ABS warning. **CRITICAL DISTRACTOR — do not fall for this:** trailer rear corners have bright marker/clearance lamps (red and amber) on the sill BELOW the "ABS" stamp that are normally LIT with the running lights and bloom brightly at night. Those glowing lamps are NOT the ABS lamp — proximity of an "ABS" stamp in the same frame does NOT make a nearby glowing marker lamp an ABS warning. If the only illuminated lamps are a few inches or more away from the "ABS" text (on the sill, in the lamp cluster, or below the corner), the ABS lamp is OFF — do NOT flag. The lens directly touching the "ABS" stamp being dark = OFF, even when other lamps in the same corner are blazing. If you are not certain the lamp is actively emitting light, treat it as OFF and do NOT flag (default to PASS). An illuminated ABS lamp IS out-of-service (see below): report it with oos=true. Format: "(M:SS) ABS malfunction lamp on (49 CFR 393.55)". PLACEMENT — the ABS lamp is a required inspection area ("ABS lamp"): if it is visible and OFF, add "ABS lamp" to "checked_clean"; if it is visible and genuinely lit, report it as an issue (do NOT also list it in checked_clean); if the ABS lamp / "ABS" label was NEVER filmed, put "ABS lamp" in "missing_areas" — an un-filmed ABS lamp makes the inspection INCOMPLETE and FAILs it.
  - Windshield: small stone chips outside the driver's line of sight are NOT a defect; only flag long/spreading cracks or chips in the swept area.
  - Mirrors: check the inner REFLECTIVE GLASS face, not just the housing/back — cracks or de-silvering can be on the glass side only. Keep "Mirrors" in "checked_clean" when the mirrors are filmed, but if the inner glass face of one or more mirrors was not clearly shown, add that to "what_was_not_visible" (e.g. "Driver-side mirror glass face"); do NOT move "Mirrors" to "missing_areas" over an un-shown inner side. A cracked/broken mirror (glass or housing) is an advisory, never OOS.
  - If you are unsure whether something is a defect, treat it as PASS and (if relevant) add it to "what_was_not_visible" rather than "issues".

PASS / FAIL rule — based ONLY on completeness, NOT on defects:
  The overall verdict is decided SOLELY by whether the driver filmed every required inspection area
  (see the Completeness rule below). It is NOT related to OOS or to any defect:
    - PASS = all 8 inspection areas were filmed — even if you found out-of-service or advisory defects.
    - FAIL = at least one required area was never filmed (an incomplete inspection).
  Defects NEVER change the verdict: a truck with an out-of-service defect still PASSES if every area was
  filmed, and a defect-free truck still FAILS if an area was not filmed. You still REPORT every defect you
  see (drivers must fix them) — you just don't fail the inspection over them.

"oos" labeling — for reporting only, does NOT affect PASS/FAIL:
  For EVERY issue you report, set "oos": true ONLY if that specific defect meets one of the OOS conditions
  below; otherwise set "oos": false. Only mark oos=true for something you can actually SEE meets the threshold.
  This labels the defect as out-of-service in the report so the driver knows its severity; it does NOT fail
  the inspection.

  OOS conditions (label these oos=true):
    - Tires: visibly flat / run-flat (deformed against the ground), tread or sidewall separation, a bulge from
      ply/belt separation, exposed cords/belt/ply fabric, or a sidewall cut or split deep enough to expose cords.
      A worn-smooth or BALD tread (grooves worn away) with NO exposed cords/separation is an ADVISORY, not OOS
      — see the NEVER-OOS list. (You cannot measure tread depth — never fail on a number.)
    - Brakes / brake pads: lining or pad missing, cracked off, or worn down to the metal/rivets; broken brake
      hardware. Even, adequate pad thickness is NOT OOS.
    - Air lines (air brake system): an air line that is cut, broken, disconnected, or visibly/audibly leaking.
    - Frame / chassis: a cracked, broken, or sagging frame member (surface rust or cosmetic dents are NOT OOS).
    - Under the hood: a FUEL leak is OOS. An engine-oil drip or seep is NOT OOS.
    - Lights: OOS only if a REQUIRED lamp is dead — e.g. no working brake (stop) lamps, or an inoperative
      headlamp or turn signal. A single out/dirty marker or clearance lamp is an advisory, not OOS.
    - ABS: an illuminated ABS malfunction lamp (warning indicator lit next to a legible "ABS" label) is OOS.

  NEVER OOS (always oos=false — label as an advisory, not out-of-service):
    - Wheels / rims: a cracked, bent, broken, or damaged wheel or rim, a missing/loose lug nut, or severe
      widespread corrosion. These ARE reportable violations (report them as issues), but per company policy a
      wheel/rim finding is NEVER out-of-service — even though FMCSA / CVSA criteria may treat a cracked wheel
      as OOS. Always oos=false.
    - Tires (tread wear): a worn-smooth or bald tread — the center tread grooves worn away, with no exposed
      cords, separation, or bulge. You MUST still report a clearly worn-out tire (it needs replacing), but per
      company policy worn/bald tread is an ADVISORY, never OOS. Always oos=false. (Exposed cords or separation
      are different — those stay OOS per the list above.)
    - Air / electrical lines that are merely UNSECURED or chafing against the catwalk or frame (a "secure it"
      advisory). A line that is actually cut, leaking, or disconnected is different — that stays OOS per the
      list above. The unsecured/chafing case is always oos=false.
    - Missing or expired fire extinguisher or warning triangle (regulatory item, not an OOS condition).
    - Broken, missing, or cracked mirror.
    - Low or unknown engine-oil level.
    - Windshield stone chips or short cracks outside the swept driver view.
    - Cosmetic damage, dirt, rust, mud, faded paint.

Completeness rule — a PTI must actually SHOW all 8 REQUIRED inspection areas (areas 1–8;
"Under the hood" is OPTIONAL and is NEVER counted toward completeness — leaving it unfilmed
never goes in "missing_areas" and never fails the inspection):
  Every one of the 8 required areas must end up in exactly ONE of "checked_clean" (it appears in the
  footage and looks fine), "issues" (a defect you saw), or "missing_areas" (it NEVER appears
  in any frame). "missing_areas" means the AREA WAS NOT FILMED AT ALL — the camera was never
  pointed at it. It does NOT mean a fine detail was hard to judge: if the area shows up in even
  one frame, it counts as FILMED — put it in "checked_clean" (or "issues" if you saw a defect),
  even when you cannot confirm every sub-detail (e.g. a possible hairline windshield crack or the
  far-side mirror). (EXCEPTION — "Lights": lights must be shown WORKING, not merely present, so
  visible-but-un-demonstrated lights or an un-filmed trailer rear DO belong in "missing_areas" — see
  the lights-completeness rule above.) Record those un-assessable sub-details in
  "what_was_not_visible" instead — NEVER in "missing_areas". An inspection with a non-empty
  "missing_areas" is INCOMPLETE and will be marked FAIL so the driver re-records the un-filmed
  areas — this is independent of OOS status (an incomplete video fails even with zero defects).
  A driver must not be able to PASS by simply not filming a component. Be conservative: list an
  area as missing ONLY if it truly never appears in any frame.

Process:
  - Look across ALL frames before deciding — a defect visible in one frame is still a defect.
  - Severity: rates the worst defect found, for the driver's awareness — it is SEPARATE from PASS/FAIL. CRITICAL = at least one out-of-service defect; MAJOR/MINOR = advisory defects worth fixing; NONE = no defects. (PASS/FAIL still depends only on completeness.)
  - VEHICLE IDENTIFICATION: Read visible unit numbers (painted on cab or trailer body) and license plates for the truck and trailer separately. Return one entry per distinct vehicle in "vehicles". If previous PTI history is provided, check whether prior issues are now fixed.

Output rules — the driver reads this on a phone, so be brutally short:
  - Frames from a video are labeled "Video frame at M:SS". Plain photos are labeled "Photo N".
  - For any defect you spot in a video frame, prefix the issue with that frame's timestamp in parens, e.g. "(1:14)". If the same defect spans several frames, use the timestamp where it's clearest. For defects only seen in a Photo (not a video frame), do NOT add a timestamp prefix.
  - Each issue is an OBJECT with three fields: "text", "evidence", and "oos".
    "text" = ≤ 8 words plain English, then ONE CFR citation in parens. Format: "(M:SS) <short defect> (49 CFR <section>)" for video defects, or "<short defect> (49 CFR <section>)" for photo defects.
    Good text: "(1:14) Cracked steer rim (49 CFR 393.205(a))", "Trailer plate missing (49 CFR 393.17)".
    Bad text:  "Cracked rim on passenger side steer wheel (49 CFR 393.205(a), 396 Appendix G, Item 1.a.1)".
    If no clear CFR applies (e.g. minor cosmetic damage), omit the CFR parens entirely (but keep the timestamp).
    "evidence" = a sentence (20–200 chars) describing WHAT YOU ACTUALLY SAW on the frame — specific location, shape, size, contrast. Do NOT restate the defect; describe the visual observation that proves it. If you cannot write a concrete evidence sentence with specific location and visual contrast, DO NOT include the issue.
    Good evidence: "Round dark cavity ~1cm wide on rightmost tread block of inner dual, recessed below surrounding rubber".
    Bad evidence (DO NOT produce): "Tire is worn", "Visible damage", "Severe wear visible", "Tread depth low", "Shoulder is smooth".
    "oos" = true/false per the OOS conditions above — true ONLY if this defect places the vehicle out of service, otherwise false.
  - "checked_clean": list which inspection areas you actually saw and verified are fine.
    Use ONLY these component labels (one per inspection area):
      "Brake pads", "Lights", "Tires", "Mirrors",
      "Windshield", "Air lines", "Frame", "ABS lamp", "Under hood".
    ("Under hood" is the only optional area — include it here when the engine bay was filmed and looks fine.)
    Each entry is just the component name — no timestamps, no extra text.
    Example: ["Tires", "Mirrors", "Lights", "Windshield"].
    Omit any component you couldn't see clearly. Don't put a component in both "issues" and "checked_clean".
  - "missing_areas": of the 8 REQUIRED inspection areas, ONLY those the driver never filmed at all (the area
    does not appear in a single frame). If an area shows up in even one frame, it is NOT missing —
    put it in "checked_clean" or "issues", never here, even if a fine detail was unclear.
    (Lights exception: also put "Lights" here when the lights were never shown WORKING or the trailer
    rear was never filmed — un-demonstrated lights count as incomplete; see the lights-completeness rule.)
    Use ONLY these component labels (one per area) — NEVER "Under hood" (it is optional, see above):
      "Brake pads", "Lights", "Tires", "Mirrors",
      "Windshield", "Air lines", "Frame", "ABS lamp".
    An area goes here ONLY if it is not in "checked_clean" and not covered by an "issue". Empty list
    means the inspection was complete. This drives the INCOMPLETE → FAIL rule above, so be accurate.
  - "what_was_not_visible": at most 5 short items, only the most important ones. Don't list every PTI area you didn't see — just the ones a driver could reasonably re-shoot.
    Describe the specific un-assessable detail (e.g. "Passenger-side mirror glass", "Windshield crack detail", "Inner trailer dual tread") — NEVER a bare inspection-area label like "Frame" or "Windshield" on its own (a bare label means the whole area wasn't filmed, which belongs in "missing_areas").
    DO NOT include "Trailer license plate" or "Trailer unit number" — drivers are not required to film these.
  - DO NOT list trailer license plate or trailer unit number absence as an issue. Drivers are not required to show them. (You may still fill them in the "vehicles" section if they happen to be visible.)
  - "advice": ONE short sentence (≤ 15 words) with the action to take. No regulations.
  - "confidence": "HIGH" if frames are clear and defects are obvious; "MEDIUM" if some calls are borderline; "LOW" if the video is dark/blurry/short.
  - "image_quality": "GOOD" / "FAIR" / "POOR" based on lighting, focus, framing.

Respond ONLY with a raw JSON object — no markdown, no code fences:
{
  "status": "PASS" or "FAIL",
  "severity": "NONE" or "MINOR" or "MAJOR" or "CRITICAL",
  "confidence": "HIGH" or "MEDIUM" or "LOW",
  "image_quality": "GOOD" or "FAIR" or "POOR",
  "issues": [
    {
      "text": "(M:SS) short defect (49 CFR ...)",
      "evidence": "what you actually saw — location, shape, contrast, 20-200 chars",
      "oos": true or false
    }
  ],
  "checked_clean": ["Tires", "Lights", ...],
  "missing_areas": ["Brake pads", ...],
  "fire_extinguisher_shown": true or false,
  "what_was_not_visible": ["short item", ...],
  "advice": "one short sentence",
  "vehicles": [
    {
      "type": "truck" or "trailer",
      "unit_number": "visible unit number or null",
      "plate": "visible license plate or null"
    }
  ]
}"""


def _build_history_text(history: list[dict]) -> str | None:
    if not history:
        return None
    lines = ["Previous PTI submissions for this driver (most recent first):"]
    for i, entry in enumerate(history, 1):
        status = "PASS" if entry.get("passed") else "FAIL"
        severity = entry.get("severity", "?")
        submitted_at = entry.get("submitted_at", "?")
        date = submitted_at.strftime("%Y-%m-%d") if hasattr(submitted_at, "strftime") else str(submitted_at)[:10]
        unit = entry.get("unit_number") or "unknown unit"
        result = entry.get("result_json")
        issues = []
        if result:
            try:
                parsed = json.loads(result)
                issues = parsed.get("issues", [])
            except Exception:
                pass
        issue_texts = [
            (i.get("text") or "").strip() if isinstance(i, dict) else str(i).strip()
            for i in issues
        ]
        issue_texts = [t for t in issue_texts if t]
        issue_text = "; ".join(issue_texts) if issue_texts else "none"
        lines.append(f"  {i}. {date} — {status} ({severity}) on {unit}. Issues: {issue_text}")
    return "\n".join(lines)


def _probe_duration(video_path: str) -> float | None:
    """Return video duration in seconds via ffprobe, or None if it can't be read."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        raise RuntimeError("ffprobe not found on PATH (ffmpeg must be installed)")
    try:
        return float((proc.stdout or "").strip())
    except ValueError:
        logging.warning(
            f"ffprobe could not read duration for {os.path.basename(video_path)}: "
            f"{proc.stderr.strip()[:200]}"
        )
        return None


def extract_frames(video_path: str, interval_seconds: float = PTI_FRAME_INTERVAL) -> list[tuple[float, str]]:
    """Sample one JPEG frame every ``interval_seconds`` using ffmpeg.

    Returns ``[(timestamp_seconds, jpg_path), ...]``. ffmpeg decodes the stream
    once sequentially (far faster than per-frame seeking), caps the longest side
    at 1280px (never upscaling), and JPEG-encodes in one pass. Raises
    ``VideoTooLongError`` if the clip exceeds ``MAX_VIDEO_DURATION``.
    """
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video file not found: {video_path}")

    duration = _probe_duration(video_path)
    if duration is not None and duration > MAX_VIDEO_DURATION:
        raise VideoTooLongError(duration)

    temp_dir = tempfile.mkdtemp(prefix="pti_frames_")
    pattern = os.path.join(temp_dir, "frame_%05d.jpg")
    fps = 1.0 / interval_seconds
    vf = (
        f"fps={fps:g},"
        "scale='min(1280,iw)':'min(1280,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2"
    )
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
        "-i", video_path,
        "-vf", vf,
        "-frames:v", str(MAX_FRAMES),
        "-q:v", "2",
        pattern,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise RuntimeError("ffmpeg not found on PATH (ffmpeg must be installed)")
    if proc.returncode != 0:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise RuntimeError(f"ffmpeg frame extraction failed: {proc.stderr.strip()[:300]}")

    files = sorted(glob.glob(os.path.join(temp_dir, "frame_*.jpg")))
    if not files:
        shutil.rmtree(temp_dir, ignore_errors=True)
        logging.warning(f"ffmpeg produced no frames for {os.path.basename(video_path)}")
        return []

    saved = [(i * interval_seconds, path) for i, path in enumerate(files)]
    dur_text = f"{duration:.1f}s" if duration is not None else "unknown duration"
    logging.info(
        f"Extracted {len(saved)} frames from {os.path.basename(video_path)} "
        f"({dur_text} @ {interval_seconds}s interval)"
    )
    return saved


def _format_timestamp(seconds: float) -> str:
    """Seconds → ``M:SS`` for the per-frame labels Gemini reads (e.g. 65.0 → "1:05")."""
    total = int(round(seconds))
    return f"{total // 60}:{total % 60:02d}"


def call_gemini(frames: list[tuple[float, str]], history: list[dict] | None = None, api_key: str | None = None):
    client = genai.Client(api_key=_resolve_api_key(api_key))
    n = len(frames)
    use_file_api = n > FILE_API_THRESHOLD
    uploaded_files = []

    # Label each frame with its real video position ("Video frame at M:SS") so the
    # model can cite an accurate timestamp. Without this it can't know the position
    # and may grab a burned-in clock overlay instead.
    labels = [f"Video frame at {_format_timestamp(ts)}" for ts, _ in frames]

    try:
        parts = []
        if use_file_api:
            logging.info(f"Uploading {n} frames via File API (parallel)...")
            frame_tuples = [("image/jpeg", path, labels[i]) for i, (_, path) in enumerate(frames)]
            with ThreadPoolExecutor(max_workers=8) as pool:
                futs = {pool.submit(_upload_one, client, path, mime, label): idx
                        for idx, (mime, path, label) in enumerate(frame_tuples)}
                results = [None] * n
                for fut in as_completed(futs):
                    results[futs[fut]] = fut.result()
            uploaded_files = results
            for i, uf in enumerate(uploaded_files):
                parts.append(genai_types.Part.from_uri(file_uri=uf.uri, mime_type="image/jpeg"))
                parts.append(labels[i])
        else:
            for i, (_, path) in enumerate(frames):
                with open(path, "rb") as f:
                    parts.append(genai_types.Part.from_bytes(data=f.read(), mime_type="image/jpeg"))
                parts.append(labels[i])

        history_text = _build_history_text(history or [])
        if history_text:
            parts.append(history_text)
        parts.append(f"Analyze all {n} frames above as a single PTI inspection and return the JSON result.")

        response = client.models.generate_content(
            model=_active_model,
            config=genai_types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0,
                response_mime_type="application/json",
            ),
            contents=parts,
        )
        return response
    finally:
        if uploaded_files:
            _delete_files_background(client, uploaded_files)


def call_gemini_photos(
    images: list[tuple],
    history: list[dict] | None = None,
    system_prompt: str | None = None,
    closing: str | None = None,
    api_key: str | None = None,
):
    """images: list of (file_path, mime_type) or (file_path, mime_type, label).
    Label is shown to Gemini after each image — e.g. "Video frame at 1:14" or "Photo 2".
    Above FILE_API_THRESHOLD images, files are uploaded via the File API instead of sent inline.

    ``system_prompt``/``closing`` override the default full-PTI instructions — used by
    ``call_gemini_tires`` to run a narrow, single-purpose pass over the same images.
    ``api_key`` selects which key to use (the failover loop passes each in turn).
    """
    client = genai.Client(api_key=_resolve_api_key(api_key))
    n = len(images)
    use_file_api = n > FILE_API_THRESHOLD
    uploaded_files = []

    # Normalise to (path, mime_type, label) triples
    triples = []
    for i, item in enumerate(images):
        if len(item) == 3:
            triples.append(item)
        else:
            path, mime_type = item
            triples.append((path, mime_type, f"Photo {i + 1} of {n}"))

    try:
        parts = []
        if use_file_api:
            logging.info(f"Uploading {n} images via File API (parallel)...")
            with ThreadPoolExecutor(max_workers=8) as pool:
                futs = {pool.submit(_upload_one, client, path, mime, label): idx
                        for idx, (path, mime, label) in enumerate(triples)}
                results = [None] * n
                for fut in as_completed(futs):
                    results[futs[fut]] = fut.result()
            uploaded_files = results
            for uf, (_, mime_type, label) in zip(uploaded_files, triples):
                parts.append(genai_types.Part.from_uri(file_uri=uf.uri, mime_type=mime_type))
                parts.append(label)
        else:
            for path, mime_type, label in triples:
                with open(path, "rb") as f:
                    parts.append(genai_types.Part.from_bytes(data=f.read(), mime_type=mime_type))
                parts.append(label)

        history_text = _build_history_text(history or [])
        if history_text:
            parts.append(history_text)
        parts.append(closing or f"Analyze all {n} image(s) above as a single PTI inspection and return the JSON result.")

        response = client.models.generate_content(
            model=_active_model,
            config=genai_types.GenerateContentConfig(
                system_instruction=system_prompt or SYSTEM_PROMPT,
                temperature=0,
                response_mime_type="application/json",
            ),
            contents=parts,
        )
        return response
    finally:
        if uploaded_files:
            _delete_files_background(client, uploaded_files)


# The SECOND Gemini pass, which looks at the same frames as the broad one and judges
# ONLY tire tread wear. The broad PTI pass juggles 8 areas across ~150+ frames and
# reliably overlooks a single worn tire (attention dilution); given the one job, the
# model attends to every tire.
#
# **It is two calls, and that split is the whole thing.** Asked to inspect, the model
# under-reports: on JRD unit 2456's 2026-08-26 clip -- a trailer axle worn until the
# rib grooves were hairlines flush with the tread -- an inspector-framed pass returned
# `tire_defect: false` over all 325 frames, over a 41-frame close-up window, over 19
# frames, over 6, and over a 2-frame pair against the deepest tire in the clip. Asked
# to DESCRIBE the same frames with no verdict to reach, the same model called those
# grooves "almost flush on a flat, smoothed tread face with virtually no open channel
# shadow" and ranked them last, every run. So the image call only observes, and a
# second, text-only call applies the policy to what it wrote.
#
# What each half must keep:
#  - The survey names no verdict and asks for no decision. Wording that invites one
#    ("report", "flag", "is this acceptable", "when in doubt PASS") is what flipped the
#    same observation from "flush" to "open channel", so it stays out.
#  - The survey describes the CENTER band and the SHOULDER in SEPARATE fields. With one
#    field they merge, and the outermost rib -- smoother by design on every commercial
#    tire -- reads as wear: a healthy stack of spares at 1:39 came back "shallow and
#    close to flush", indistinguishable from the genuinely worn tire.
#  - The decision reads text only. It never gets the images back, because re-looking is
#    exactly the step that fails.
#  - Depth, never presence. A rib groove leaves a traceable wavy line right up until it
#    is gone, so "you can still see a groove" -- the old rib-tire carve-out -- is what
#    read a worn-out rib tire as fine. An open channel is dark, wide and shadowed
#    inside; a worn-out one is a faint line flush with a flat face.
#
# Two guards the old prompt used are deliberately absent. It required the contrast to
# be against the adjacent dual IN THE SAME FRAME -- which a close-up of a worn tire
# never has, so the one case this pass exists for was the case it could not report --
# and it said "never flag all tires worn", though an axle wears out as a set and a
# matched pair is exactly what gets filmed up close. What they guarded against, a
# uniform rib set that merely looks shallow, is now covered by the depth test and by
# the survey's rule to skip distant, oblique and wet tires.
#
# Evidence stays CONCRETE (a flush line where the deepest tire has an open, shadowed
# channel) so promoted issues survive utils.pti_processor.filter_hallucinated_issues.
TIRE_SURVEY_PROMPT = """You are analysing TIRE TREAD DEPTH in frames from a commercial-truck walkaround. Ignore everything else in \
the frames (lights, brakes, frame, leaks, mirrors, ABS) — tread depth is the only subject. This is an \
OBSERVATION task: describe and rank what you see. Do not judge whether any tire is acceptable, and do not \
decide anything.

SURVEY. Go through the frames and list every tire whose tread face is shown CLOSE enough and HEAD-ON enough \
to judge: its tread fills a good part of the frame and you can see across the center of the tread. For each \
one record:
 - the timestamp,
 - "center_grooves": what the grooves in the CENTER of the tread look like — how DARK they are compared with \
the rubber beside them, how WIDE, and whether you can see DOWN INTO an open channel with shadow inside, or \
whether the line lies FLUSH with a flat tread face. The CENTER band only: the outermost rib at each SHOULDER \
is smoother and shallower by design on every commercial tire, so say nothing about it here.
 - "shoulder_note": anything you noticed about the shoulder, kept separate so it cannot be mistaken for the \
center.
Leave out tires that are small in the frame, seen side-on down the length of the trailer, or reduced to a \
foreshortened edge behind an outer dual — depth cannot be read from those, and a worn-looking edge at a \
distance is viewing angle, not wear. Leave out WET tires unless the view is close, head-on and in focus: \
water fills the grooves and a wet sheen reads as falsely bald.

RANK the tires you listed from MOST remaining CENTER tread depth to LEAST. Absolute tread depth cannot be \
read from a photo, but a difference in depth can, so the ranking is the point of this pass. Rank rib tires \
against rib tires where you can — a shallow rib tread and a deep drive lug are different designs, not \
different amounts of wear.

Highway rib tires have WAVY (zigzag) circumferential grooves. As such a tire wears out the wavy bottom of \
the groove reaches the surface, so what is left is a faint wavy hairline on a flat face. Being able to trace \
a continuous wavy line says nothing about its depth — describe the DEPTH of the line, not its presence.

Be plain about what you see. If a center groove is a wide dark slot you can see into, say so. If it is a \
faint line lying flush on a flat face, say that just as plainly — this pass is only an observation, and \
nothing is decided from it here.

Also note, separately, whether the walkaround showed the tires at all (every wheel position, or only some).

Return ONLY this JSON (no prose):
{
  "survey": [
    {"timestamp": "M:SS",
     "center_grooves": "<how dark, how wide, open channel with shadow inside or flush line on a flat face>",
     "shoulder_note": "<anything about the shoulder, or empty>"}
  ],
  "ranked_most_to_least_depth": ["M:SS", "M:SS", "..."],
  "tires_fully_shown": true/false
}"""


# The policy half of the tire pass — see the note above TIRE_SURVEY_PROMPT for why
# deciding is a separate, text-only call.
TIRE_DECIDE_PROMPT = """You are applying a fleet's tire policy to TREAD OBSERVATIONS that were already made from a pre-trip \
inspection video. There are no images here — you are given the observations as JSON and you judge the \
descriptions, nothing else. Do not imagine detail that is not written down, and do not soften an observation \
because it sounds severe: the looking has been done, your job is only to apply the rule to it.

Each entry is one tire: the timestamp it was seen at, "center_grooves" describing the grooves in the CENTER \
of its tread, and "shoulder_note" describing its shoulder. The entries are ranked from most remaining center \
tread depth to least.

THE RULE. Report a tire when its "center_grooves" describes grooves that have LOST THEIR DEPTH — flush, \
almost flush, nearly flush, or approaching flush with the tread face; lying on a flat or smoothed face; no \
visible depth; no, virtually no, or minimal shadow inside; worn away. That tire's tread grooves are gone and \
the driver needs to replace it.

Do NOT report:
 - a tire whose "center_grooves" says the grooves are open channels with shadow inside — however SHALLOW, \
NARROW, MODERATE or low the same sentence also calls them. A shallow open channel is a working tire; only a \
groove that has lost its depth is a finding.
 - a tire on the strength of its "shoulder_note". The outermost rib at each shoulder is smoother and \
shallower by design on every commercial tire, so a flat, smooth or worn shoulder is normal and is never \
itself a finding.
 - a tire that is merely lower in the ranking than another. The ranking tells you where to look; only the \
description decides.
When a description points both ways, take the phrase about DEPTH and SHADOW as the answer: "shallow but open \
with visible shadow" is not reported, "shallow and almost flush with minimal shadow" is.

For each tire you report, write:
 - "text": "(M:SS) <the tire as the observation identifies it, e.g. trailer tire>: center tread worn smooth \
(49 CFR 393.75)"
 - "evidence": the entry's own center_grooves observation, set against the top-ranked entry's — e.g. "center \
tread grooves lie almost flush on a flat face with virtually no shadow inside, against the tire at 1:04 \
whose center channels are wide and deeply shadowed". Quote what was observed; do not add vague conclusions \
like "worn", "severe wear", "tread is low", or "tire is worn", and never quote a tread-depth number or "wear \
bars".

CLASSIFICATION — per company policy, worn/bald tread is an ADVISORY, never out-of-service: set "oos": false \
on EVERY finding. (Exposed cords, tread/sidewall separation, a bulge, or a flat would be out-of-service, but \
none of those are what this pass looks at.) The driver must still see the worn tire so they replace it — \
finding it matters; it is just not OOS.

Return ONLY this JSON (no prose):
{
  "tire_defect": true/false,
  "issues": [
    {"text": "(M:SS) <tire>: center tread worn smooth (49 CFR 393.75)",
     "evidence": "<the observation, >=20 chars>",
     "oos": false}
  ]
}"""


def _call_gemini_tire_decision(survey: dict, api_key: str | None = None):
    """Apply the tire policy to the survey's own words. No images, by design.

    Handing the frames back is the step that fails (see the note above
    TIRE_SURVEY_PROMPT), so this call never sees them: it reads the descriptions the
    survey wrote and decides which of them describe a groove that has lost its depth.
    Costs ~1k tokens next to the survey's ~350k.
    """
    client = genai.Client(api_key=_resolve_api_key(api_key))
    return client.models.generate_content(
        model=_active_model,
        config=genai_types.GenerateContentConfig(
            system_instruction=TIRE_DECIDE_PROMPT,
            temperature=0,
            response_mime_type="application/json",
        ),
        contents=[json.dumps(survey, ensure_ascii=False),
                  "Apply the rule to these observations and return the JSON result."],
    )


def call_gemini_tires(images: list[tuple], history: list[dict] | None = None, api_key: str | None = None):
    """Focused second pass: judge ONLY tire tread wear over the same images.

    Two calls — an observation pass over the frames, then a text-only decision on what
    it observed. Returns the DECISION's raw Gemini response, shaped like the old
    single-call one (`tire_defect` + `issues`), so callers are unchanged; see
    utils.pti_processor.merge_tire_pass. Both calls use the same key, so the failover
    in _call_gemini_with_retry retries the pair.
    """
    n = len(images)
    survey = parse_result(call_gemini_photos(
        images,
        history=history,
        system_prompt=TIRE_SURVEY_PROMPT,
        closing=f"Survey all {n} image(s) above for tire tread depth and return the survey JSON.",
        api_key=api_key,
    ))
    logging.info(f"Tire survey: {len(survey.get('survey') or [])} tire(s) described")
    return _call_gemini_tire_decision(survey, api_key=api_key)


def delete_frames(frames: list[tuple[float, str]]) -> None:
    for _, path in frames:
        try:
            os.remove(path)
        except OSError:
            pass
    if frames:
        try:
            os.rmdir(os.path.dirname(frames[0][1]))
        except OSError:
            pass


def parse_result(response) -> dict:
    text = getattr(response, "text", None)
    if not text:
        reason = "unknown"
        try:
            reason = str(response.candidates[0].finish_reason)
        except Exception:
            pass
        raise ValueError(f"Gemini returned no text (finish_reason={reason})")
    return json.loads(text)


def print_result(response) -> None:
    print("\n--- RESULT ---")
    try:
        parsed = parse_result(response)
        print(json.dumps(parsed, indent=2))
    except json.JSONDecodeError:
        print("Warning: Response is not valid JSON. Raw output:")
        print(response.text)

    usage = response.usage_metadata
    print("\n--- USAGE ---")
    print(f"  Input tokens:  {usage.prompt_token_count}")
    print(f"  Output tokens: {usage.candidates_token_count}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m utils.gemini <path_to_video>")
        sys.exit(1)

    video_path = sys.argv[1]

    print(f"Extracting frames every {PTI_FRAME_INTERVAL}s from: {video_path}")
    frames = extract_frames(video_path)

    if not frames:
        print("Error: No frames could be extracted from the video.")
        sys.exit(1)

    print(f"\nSending {len(frames)} frames to Gemini...")
    try:
        response = call_gemini(frames)
    finally:
        delete_frames(frames)
        print("Temporary frame files deleted.")

    print_result(response)
