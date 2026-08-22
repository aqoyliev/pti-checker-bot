"""Put the grandfathered Gemini key in front of the production key list.

Why this exists: Google grandfathers model access per *account*. After the
2026-08-20 cutoff all four production keys 404 on ``gemini-2.5-pro`` -- the model
every PTI prompt is tuned against, and the cheaper one -- while the older key in
the local ``.env`` still serves it. Until that key is in ``GEMINI_API_KEYS`` the
bot falls through to ``gemini-3.1-pro-preview`` on every inspection: it works,
but at the price the switch was meant to avoid.

It goes FIRST so the common call succeeds on the first try instead of spending a
404 round-trip per dead key. (``utils.pti_processor`` learns which keys serve
which model anyway, so order is an optimisation, not a correctness fix.)

Run it yourself -- it reads a secret and writes production config, so it is not
something to hand to an agent:

    py -3.11 scripts/add_gemini_key.py            # show what would change
    py -3.11 scripts/add_gemini_key.py --apply    # write it

Only the last four characters of any key are ever printed.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import sys

SERVICE = "pti-checker-bot"
ENV_FILE = pathlib.Path(__file__).resolve().parent.parent / ".env"


def _tail(k: str) -> str:
    return f"...{k[-4:]}"


def _railway() -> str:
    """Full path to the CLI -- on Windows it is a .cmd shim that bare
    subprocess (no shell) will not find on PATH."""
    exe = shutil.which("railway")
    if not exe:
        sys.exit("railway CLI not found on PATH.")
    return exe


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually write the variable")
    args = ap.parse_args()

    if not ENV_FILE.exists():
        sys.exit(f"{ENV_FILE} not found -- run this from the repo checkout.")

    local = ""
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("GEMINI_API_KEY="):
            local = line.partition("=")[2].strip()
    if not local:
        sys.exit("No GEMINI_API_KEY in .env -- nothing to add.")

    raw = subprocess.run(
        [_railway(), "variables", "--service", SERVICE, "--json"],
        capture_output=True, text=True, timeout=120)
    if raw.returncode != 0:
        sys.exit(f"railway variables failed:\n{raw.stderr[-400:]}")
    cur = json.loads(raw.stdout)
    prod = [p.strip() for p in
            (cur.get("GEMINI_API_KEYS") or cur.get("GEMINI_API_KEY") or "")
            .replace(",", " ").split() if p.strip()]

    print(f"current production keys : {[_tail(k) for k in prod]}")
    print(f"local .env key          : {_tail(local)}")

    if local in prod:
        if prod[0] == local:
            print("\nAlready first in the list -- nothing to do.")
        else:
            print("\nPresent but not first; reordering is optional.")
        return

    new = [local] + prod
    print(f"new order               : {[_tail(k) for k in new]}")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write it.")
        return

    w = subprocess.run(
        [_railway(), "variables", "--service", SERVICE,
         "--set", "GEMINI_API_KEYS=" + ",".join(new)],
        capture_output=True, text=True, timeout=300)
    out = (w.stdout + w.stderr).replace(local, "<KEY>")
    if w.returncode != 0:
        sys.exit(f"failed:\n{out[-500:]}")
    print("\nSet. Railway redeploys automatically; the bot will use "
          "gemini-2.5-pro on the next inspection.")


if __name__ == "__main__":
    main()
