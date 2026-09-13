#!/usr/bin/env python3
"""Build the two fleet PTI reports as PDFs, for any fleet and any date window.

    python scripts/fleet_report.py --fleet jrd-pti --last-week
    python scripts/fleet_report.py --fleet jrd-pti --since 2026-08-17 --until 2026-08-24
    python scripts/fleet_report.py --fleet jrd-pti --days 7

Reads ``DATABASE_URL`` (or ``--database-url``) and writes, into ``--out``:

    <fleet>-<end>.pdf                fleet inspection statistics (one landscape page)
    <fleet>-driver-report-<end>.pdf  per-driver inspections (portrait, many pages)
    <fleet>-<end>-*.csv              the same numbers, unrounded

The window is half-open [since, until) in fleet-local time, so a week is
Monday 00:00 up to the next Monday 00:00 with no double-counted midnight.

Nothing here writes to the database -- every statement is a SELECT.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import html
import importlib.util
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg  # noqa: E402

# Loaded by path, not as ``utils.report_scoring``: importing the ``utils``
# package runs data/config.py, which demands BOT_TOKEN and every other bot
# secret. A report only ever needs a database URL, and requiring a bot token to
# print a PDF is how a reporting job ends up holding credentials it cannot use.
_scoring_path = Path(__file__).resolve().parent.parent / "utils" / "report_scoring.py"
_spec = importlib.util.spec_from_file_location("report_scoring", _scoring_path)
report_scoring = importlib.util.module_from_spec(_spec)
# dataclasses resolves annotations through sys.modules, so register first.
sys.modules[_spec.name] = report_scoring
_spec.loader.exec_module(report_scoring)
score_inspection = report_scoring.score_inspection

CHROME_CANDIDATES = (
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


# ---------------------------------------------------------------- fetching

async def fetch(dsn: str, since_utc: datetime, until_utc: datetime) -> dict:
    conn = await asyncpg.connect(dsn)
    try:
        groups = await conn.fetch(
            """
            SELECT group_id, unit_number, title,
                   COALESCE(setup_complete, FALSE) AS setup_complete,
                   COALESCE(is_active, TRUE)       AS is_active
              FROM groups
            """
        )
        drivers = await conn.fetch(
            "SELECT group_id, user_id, name FROM group_drivers"
        )
        window = await conn.fetch(
            """
            SELECT id, group_id, user_id, driver_name, submitted_at,
                   passed, unit_number, result_json
              FROM pti_log
             WHERE submitted_at >= $1 AND submitted_at < $2
             ORDER BY submitted_at
            """,
            since_utc, until_utc,
        )
        # All-time totals answer "has this truck *ever* sent one?", which a
        # windowed count cannot: a unit silent last week may still be a unit
        # that has never once been inspected, and only one of those is news.
        alltime = await conn.fetch(
            """
            SELECT group_id, COUNT(*) AS n, MAX(submitted_at) AS last_at
              FROM pti_log GROUP BY group_id
            """
        )
        return {
            "groups": [dict(r) for r in groups],
            "drivers": [dict(r) for r in drivers],
            "window": [dict(r) for r in window],
            "alltime": {r["group_id"]: dict(r) for r in alltime},
        }
    finally:
        await conn.close()


# ------------------------------------------------------------- aggregating

def build(data: dict, tz: ZoneInfo, since: date, until: date) -> dict:
    """Turn raw rows into everything both reports print."""
    groups = {g["group_id"]: g for g in data["groups"]}
    drivers_by_group = defaultdict(list)
    driver_name = {}
    for d in data["drivers"]:
        drivers_by_group[d["group_id"]].append(d)
        driver_name[(d["group_id"], d["user_id"])] = d["name"]

    def local_day(ts: datetime) -> date:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(tz).date()

    inspections = []
    for row in data["window"]:
        gid = row["group_id"]
        g = groups.get(gid, {})
        # Inactive groups are excluded everywhere else here -- the silent list
        # and the driver list both skip them -- so a retired truck's
        # submission cannot count either, or the coverage headline becomes a
        # fraction whose two halves were measured over different fleets and
        # can read over 100%. A window row with no `groups` row at all is
        # kept: unknown is not the same as retired.
        if not g.get("is_active", True):
            continue
        s = score_inspection(row["result_json"])
        inspections.append({
            "day": local_day(row["submitted_at"]),
            "group_id": gid,
            "user_id": row["user_id"],
            "unit": g.get("unit_number") or row["unit_number"] or "no unit no.",
            "name": (driver_name.get((gid, row["user_id"]))
                     or row["driver_name"] or f"user {row['user_id']}"),
            "passed": bool(row["passed"]),
            "score": s,
        })

    # --- per-driver rollup, ranked the way the fleet reads it: real PTIs first
    per_driver = defaultdict(list)
    for i in inspections:
        per_driver[(i["group_id"], i["user_id"])].append(i)

    driver_rows = []
    for key, items in per_driver.items():
        real = [i for i in items if i["score"].is_real]
        scores = [i["score"].score for i in items]
        driver_rows.append({
            "key": key,
            "name": items[0]["name"],
            "unit": items[0]["unit"],
            "submissions": len(items),
            "real": len(real),
            "passed": sum(1 for i in items if i["passed"]),
            "avg": round(sum(scores) / len(scores)) if scores else 0,
            "best": max(scores) if scores else 0,
            "items": sorted(items, key=lambda i: i["day"]),
        })
    # Registered drivers who sent nothing at all still belong on the list --
    # a silent driver is the whole point of a compliance report.
    for (gid, uid), name in driver_name.items():
        if (gid, uid) in per_driver:
            continue
        g = groups.get(gid, {})
        if not g.get("is_active", True):
            continue
        driver_rows.append({
            "key": (gid, uid), "name": name,
            "unit": g.get("unit_number") or "no unit no.",
            "submissions": 0, "real": 0, "passed": 0, "avg": 0, "best": 0,
            "items": [],
        })
    driver_rows.sort(key=lambda r: (-r["real"], -r["passed"], -r["avg"], r["name"]))

    # --- per-unit
    units_with = {i["group_id"] for i in inspections}
    active_groups = [g for g in groups.values() if g.get("is_active", True)]
    silent = []
    for g in active_groups:
        if g["group_id"] in units_with:
            continue
        at = data["alltime"].get(g["group_id"])
        silent.append({
            "unit": g.get("unit_number") or "no unit no.",
            "drivers": len(drivers_by_group.get(g["group_id"], [])),
            "setup": bool(g.get("setup_complete")),
            "ever": int(at["n"]) if at else 0,
            "last": local_day(at["last_at"]) if at and at["last_at"] else None,
        })
    silent.sort(key=lambda u: (u["ever"] > 0, u["unit"]))
    never_ever = [u for u in silent if u["ever"] == 0]

    # --- daily series across the whole window, including the zero days
    days, cur = [], since
    while cur < until:
        days.append(cur)
        cur += timedelta(days=1)
    by_day = defaultdict(list)
    for i in inspections:
        by_day[i["day"]].append(i)
    daily = [{
        "day": d,
        "n": len(by_day.get(d, [])),
        "units": len({i["group_id"] for i in by_day.get(d, [])}),
        "real": sum(1 for i in by_day.get(d, []) if i["score"].is_real),
    } for d in days]

    klass_mix = defaultdict(int)
    for i in inspections:
        klass_mix[i["score"].klass] += 1

    scores = [i["score"].score for i in inspections]
    return {
        "inspections": inspections,
        "driver_rows": driver_rows,
        "silent": silent,
        "never_ever": never_ever,
        "daily": daily,
        "klass_mix": dict(klass_mix),
        "totals": {
            "inspections": len(inspections),
            "real": sum(1 for i in inspections if i["score"].is_real),
            "passed": sum(1 for i in inspections if i["passed"]),
            "avg": round(sum(scores) / len(scores)) if scores else 0,
            "drivers_total": sum(
                1 for gid, _ in driver_name
                if groups.get(gid, {}).get("is_active", True)),
            "drivers_sent": len({(i["group_id"], i["user_id"]) for i in inspections}),
            "units_total": len(groups),
            "units_active": len(active_groups),
            "units_sent": len(units_with),
            "silent": len(silent),
            "never_ever": len(never_ever),
        },
    }


# ------------------------------------------------------------------ typeface

# The typeface travels inside the document. Rendering happens on whatever
# Chromium the host has, and a slim container image ships no fonts at all --
# fontconfig then falls back to a last-resort face, which is how a page of
# numbers turns into a page nobody can read. Embedding it means the PDF is the
# same document on a laptop, on Railway and on the next fleet's deployment.
# Inter is SIL OFL 1.1; the licence ships beside the files.
_FONT_DIR = Path(__file__).resolve().parent / "report_fonts"
_SUBSETS = {
    "latin":
        "U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,"
        "U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,"
        "U+2212,U+2215,U+FEFF,U+FFFD",
    "latin-ext":
        "U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,"
        "U+0304,U+0308,U+0329,U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,"
        "U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF",
}


@lru_cache(maxsize=1)
def font_css() -> str:
    """Inter, base64'd into the page. A missing file degrades to the stack."""
    faces = []
    for weight in (400, 600, 700):
        for subset, ranges in _SUBSETS.items():
            f = _FONT_DIR / f"Inter-{weight}-{subset}.woff2"
            if not f.exists():
                continue
            b64 = base64.b64encode(f.read_bytes()).decode("ascii")
            faces.append(
                "@font-face{font-family:Inter;font-style:normal;"
                "font-display:block;"
                f"font-weight:{weight};unicode-range:{ranges};"
                f"src:url(data:font/woff2;base64,{b64}) format('woff2')}}"
            )
    return "".join(faces)


# ---------------------------------------------------------------- the look

# Both sheets are fixed-size paper, not a responsive page, so every column is
# sized in px against the printable box and the charts are emitted at exactly
# the width they occupy. Letter at 96dpi is 1056x816 landscape / 816x1056
# portrait; the margins below leave 965x733 and 725x952 of printable box.
#
# The statistics sheet is *one page*, and that is a budget, not a preference:
# masthead 54 + headline row 107 + charts 184 + tables 302 + legend 50, plus
# the gaps, comes to 733. STATS_ROWS is what is left over for the two bottom
# tables once everything above it has been paid for -- raise the chart, the
# type scale or the legend and rows have to come off the bottom to match.
DAY_CHART_W = 576      # the 602px chart panel, less its padding and border
DAY_CHART_H = 138
STATS_ROWS = 10        # rows per column in the sheet's two bottom tables

CSS = """
@page { size: __PAGE__; margin: __MARGIN__; }

:root{
  --ink:#0f1318; --body:#39424e; --mute:#767f8d; --faint:#9aa3b1;
  --rule:#e5e9ef; --rule-2:#ccd4de; --panel:#f7f9fb;
  --brand:#15304e; --brand-ink:#ffffff; --brand-sub:#9fb7d0;
  --g:#12684a; --o:#4f9a6f; --w:#b3800f; --r:#a8434a;
  --bar:#1c6b4c; --bar-2:#a8ccb8; --bar-0:#e2e7ec;
}
*{box-sizing:border-box;}
html,body{background:#fff;}
body{
  font-family:Inter,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,
    "DejaVu Sans",sans-serif;
  color:var(--body); background:#fff; color-scheme:light; margin:0;
  font-size:10.4px; line-height:1.45;
  font-variant-numeric:tabular-nums; font-feature-settings:"tnum" 1;
  -webkit-print-color-adjust:exact; print-color-adjust:exact;
  -webkit-font-smoothing:antialiased;
}
b,strong{font-weight:600;color:var(--ink);}
a{color:inherit;text-decoration:none;}
.g{color:var(--g);} .o{color:var(--o);} .w{color:var(--w);} .r{color:var(--r);}

/* ---------- masthead ---------- */
.mast{display:flex;align-items:center;gap:16px;background:var(--brand);
  color:var(--brand-ink);border-radius:9px;padding:11px 17px;min-height:53px;}
/* The company name is whatever the fleet calls itself, so the wordmark is
   sized to it and hard-capped: left to run, a long name squeezes the title,
   grows the band and pushes the one-page sheet onto a second page. */
.mast .mark{font-weight:700;text-transform:uppercase;padding-right:16px;
  border-right:1px solid rgba(255,255,255,.24);max-width:282px;
  line-height:1.16;display:-webkit-box;-webkit-line-clamp:2;
  -webkit-box-orient:vertical;overflow:hidden;}
.mast .mark.m1{font-size:15.5px;letter-spacing:.1em;white-space:nowrap;}
.mast .mark.m2{font-size:12.8px;letter-spacing:.08em;white-space:nowrap;}
.mast .mark.m3{font-size:10.8px;letter-spacing:.06em;}
.mast .mid{flex:1;min-width:0;}
.mast .ttl{font-size:14.5px;font-weight:600;letter-spacing:-.012em;
  white-space:nowrap;}
.mast .when{text-align:right;white-space:nowrap;}
.mast .when .ttl{font-size:12.5px;}
.mast .sub{font-size:9.3px;color:var(--brand-sub);margin-top:2px;
  letter-spacing:.015em;font-weight:400;}

/* ---------- headline numbers ---------- */
.kpis{display:flex;gap:12px;margin-top:12px;align-items:stretch;}
.hero{flex:none;width:262px;background:var(--panel);border:1px solid var(--rule);
  border-radius:8px;padding:9px 12px 10px;}
.card{flex:1;min-width:0;border:1px solid var(--rule);border-radius:8px;
  padding:9px 12px 10px;}
.k{font-size:8.4px;font-weight:600;letter-spacing:.085em;text-transform:uppercase;
  color:var(--mute);}
.card .v{font-size:23px;font-weight:700;color:var(--ink);letter-spacing:-.03em;
  line-height:1.08;margin-top:4px;}
.card .d{font-size:9.2px;color:var(--mute);margin-top:4px;line-height:1.4;}
.hero .v{font-size:29px;font-weight:700;color:var(--ink);letter-spacing:-.035em;
  line-height:1.05;margin-top:2px;}
.hero .d{font-size:9.2px;color:var(--mute);margin-top:4px;line-height:1.45;}
.track{height:7px;border-radius:4px;background:#dfe4ea;margin-top:6px;
  overflow:hidden;}
.track i{display:block;height:100%;border-radius:4px;background:var(--g);}
.track.o i{background:var(--o);} .track.w i{background:var(--w);}
.track.r i{background:var(--r);}

/* ---------- panels ---------- */
/* Fixed paper, so the columns are sized to the printable box rather than left
   to flex: the day chart has to know its own width to the pixel. */
.row{display:flex;gap:18px;margin-top:12px;align-items:stretch;}
.panel{border:1px solid var(--rule);border-radius:8px;padding:9px 12px 10px;
  min-width:0;}
.p-day{flex:none;width:602px;} .p-mix{flex:1;min-width:0;}
.p-silent{flex:none;width:446px;} .p-drv{flex:1;min-width:0;}
.d{font-size:9.2px;color:var(--mute);margin:0;}
.ph{display:flex;align-items:baseline;gap:10px;margin-bottom:7px;}
.ph h2{font-size:9.6px;font-weight:600;letter-spacing:.085em;text-transform:uppercase;
  color:var(--ink);margin:0;white-space:nowrap;}
.ph .pn{font-size:8.8px;color:var(--mute);margin:0;flex:1;min-width:0;
  text-align:right;}
.ph .pn .sw{margin-left:9px;}

/* ---------- tables ---------- */
/* table-layout:fixed with an explicit colgroup, so the same columns land on
   the same x in every block -- per-driver tables sized by their own content
   wander a few px each and the eye cannot run down the page. */
table{border-collapse:collapse;width:100%;table-layout:fixed;}
th{font-size:8.2px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;
  color:var(--mute);text-align:left;padding:0 6px 4px;white-space:nowrap;
  border-bottom:1px solid var(--rule-2);}
td{font-size:9.9px;padding:3.1px 6px;border-bottom:1px solid var(--rule);
  vertical-align:baseline;}
tbody tr:last-child td{border-bottom:none;}
td.n,th.n{text-align:right;}
td.n{white-space:nowrap;}
/* A header over a number-plus-bar column lines up with the number, not with
   the far end of the bar. */
th.b{text-align:right;padding-right:50px;}
td.u{color:var(--ink);font-weight:600;}
td.nw{white-space:nowrap;}
tr.dim td{color:var(--faint);}
tr.dim td.u{color:var(--faint);font-weight:400;}
.cols{display:flex;gap:12px;align-items:flex-start;}
.cols>*{flex:1;min-width:0;}
.pg{break-after:page;}

/* ---------- score bar ---------- */
.pct{display:inline-block;min-width:29px;text-align:right;}
.sb{display:inline-block;width:38px;height:4px;border-radius:2px;
  background:#e4e8ee;vertical-align:middle;margin-left:6px;overflow:hidden;}
.sb i{display:block;height:100%;background:var(--g);}
.sb.o i{background:var(--o);} .sb.w i{background:var(--w);}
.sb.r i{background:var(--r);}

/* ---------- quality mix ---------- */
.qbar{display:flex;height:22px;border-radius:5px;overflow:hidden;
  background:var(--bar-0);margin-bottom:9px;}
.qbar i{display:block;height:100%;}
.q1{background:var(--g);} .q2{background:var(--o);}
.q3{background:var(--w);} .q4{background:var(--r);}
.qrow{display:flex;align-items:baseline;gap:7px;font-size:9.4px;
  padding:3.1px 0;border-bottom:1px solid var(--rule);}
.qrow:last-child{border-bottom:none;}
.sw{display:inline-block;width:8px;height:8px;border-radius:2px;flex:none;}
.qn{font-weight:600;color:var(--ink);white-space:nowrap;}
.qd{flex:1;min-width:0;color:var(--mute);font-size:8.8px;}
.qv{font-weight:600;color:var(--ink);}
.qp{color:var(--mute);width:26px;text-align:right;}

/* ---------- charts ---------- */
.chart{display:block;}
.wknd{fill:#f4f6f9;}
.grid{stroke:#edf1f5;stroke-width:1;}
.axis{stroke:#ccd4de;stroke-width:1;}
.b-sub{fill:var(--bar);} .b-unit{fill:var(--bar-2);}
.b-sub.zero,.b-unit.zero{fill:var(--bar-0);}
.vlab{font-size:9px;font-weight:600;fill:#4b5563;text-anchor:middle;}
.vlab.q{fill:#9aa3b1;}
.xlab{font-size:9.4px;font-weight:600;fill:#3a434f;text-anchor:middle;}
.xlab2{font-size:7.8px;font-weight:600;fill:#9aa3b1;text-anchor:middle;
  letter-spacing:.06em;}

/* ---------- legend ---------- */
.legend{display:flex;gap:18px;margin-top:11px;}
.legend>div{flex:1;min-width:0;font-size:8.6px;color:var(--mute);
  line-height:1.45;}
.legend .lt{font-size:8.2px;font-weight:600;letter-spacing:.085em;
  text-transform:uppercase;color:var(--ink);display:block;margin-bottom:2px;}

/* ---------- driver report ---------- */
.intro{display:flex;gap:16px;margin-top:13px;}
.intro>div{flex:1;min-width:0;border:1px solid var(--rule);border-radius:8px;
  padding:9px 12px 10px;font-size:9.1px;color:var(--mute);line-height:1.5;}
.intro .lt{font-size:8.4px;font-weight:600;letter-spacing:.085em;
  text-transform:uppercase;color:var(--ink);display:block;margin-bottom:3px;}
h2.sec{font-size:9.8px;font-weight:600;letter-spacing:.085em;
  text-transform:uppercase;color:var(--ink);margin:17px 0 7px;
  padding-bottom:5px;border-bottom:1px solid var(--rule-2);}
/* A driver's card stays whole: a table header alone at the top of a page
   belongs to nobody. Nine-odd rows always fit, and "avoid" is a hint, so a
   freak 30-inspection driver still breaks rather than overflowing. */
.drv{margin-top:11px;break-inside:avoid;}
.dh{display:flex;align-items:baseline;gap:9px;padding:5px 9px;
  background:var(--panel);border:1px solid var(--rule);border-radius:6px 6px 0 0;
  border-bottom:none;break-after:avoid;}
.dh .dn{font-size:11.6px;font-weight:700;color:var(--ink);letter-spacing:-.01em;}
.dh .du{font-size:9px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;
  color:var(--mute);flex:1;min-width:0;}
.dh .st{font-size:9.2px;color:var(--mute);white-space:nowrap;}
.dh .st em{font-style:normal;font-weight:700;color:var(--ink);}
.dh .st span{margin-left:10px;}
.drv table{border:1px solid var(--rule);border-radius:0 0 6px 6px;}
.drv th{padding:4px 8px;background:#fcfdfe;}
.drv td{font-size:10.1px;padding:4.4px 8px;}
.badge{display:inline-block;font-size:8.3px;font-weight:700;letter-spacing:.06em;
  padding:1.2px 5px;border-radius:3px;}
.badge.p{background:#e3f1ea;color:#0e5c40;}
.badge.f{background:#fae9ea;color:#8f1f28;}
.chip{display:inline-block;font-size:8.6px;padding:.8px 4.5px;border-radius:3px;
  background:#f0f2f5;color:#4d5663;margin:0 3px 2px 0;white-space:nowrap;}
.chip.hot{background:#fbecec;color:#8f2630;}
.nv{font-size:8.6px;color:var(--faint);}
.ok{color:var(--faint);}
tr{break-inside:avoid;}
"""


def _page_head(title, page, margin):
    # str.replace, not %-formatting: the stylesheet is full of literal "%".
    css = CSS.replace("__PAGE__", page).replace("__MARGIN__", margin)
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<title>{html.escape(title)}</title>'
            f'<style>{font_css()}{css}</style></head><body>')


def fmt_day(d: date) -> str:
    return f"{d.day:02d} {d:%b}"


def display_name(fleet: str) -> str:
    """"jrd-pti" -> "jrd". The headline already says "pti"."""
    low = fleet.lower()
    for suffix in ("-pti", "_pti", " pti"):
        if low.endswith(suffix):
            return fleet[: -len(suffix)]
    return fleet


def band(pct: float) -> str:
    """Health letter for a percentage. Never the only signal -- the number is
    printed beside every bar it colours, so the sheet survives greyscale."""
    if pct >= 90:
        return "g"
    if pct >= 75:
        return "o"
    if pct >= 50:
        return "w"
    return "r"


def sbar(pct: int) -> str:
    w = max(0, min(100, pct))
    return f'<span class="sb {band(pct)}"><i style="width:{w}%"></i></span>'


def pct_cell(pct: int) -> str:
    """A percentage and its bar. The number sits in a fixed box so that a
    column of them lines up on the digits rather than on the bar's end."""
    return f'<span class="pct">{pct}%</span>{sbar(pct)}'


def colgroup(*widths) -> str:
    """Explicit column widths in px; None means "take what is left"."""
    cols = "".join("<col>" if w is None else f'<col style="width:{w}px">'
                   for w in widths)
    return f"<colgroup>{cols}</colgroup>"


def pctf(a: int, b: int) -> str:
    return f"{round(100 * a / b)}%" if b else "—"


def wordmark(name: str) -> str:
    """The company's name, set to fit. Three steps, picked on length: long
    names step down and are allowed a second line; past that the name is cut,
    because a wordmark that keeps growing takes the layout with it."""
    name = (name or "Fleet").strip()[:60]
    size = "m1" if len(name) <= 18 else "m2" if len(name) <= 26 else "m3"
    return f'<div class="mark {size}">{html.escape(name)}</div>'


def masthead(meta, title) -> str:
    last = meta["until"] - timedelta(days=1)
    n_days = (meta["until"] - meta["since"]).days
    span = (f'{fmt_day(meta["since"])} – {fmt_day(last)} {last.year}'
            if meta["since"].year == last.year else
            f'{fmt_day(meta["since"])} {meta["since"].year} – '
            f'{fmt_day(last)} {last.year}')
    # The wordmark already carries the company name; a scope that only repeats
    # it is noise on a page that has none to spare.
    scope = (meta.get("scope") or "").strip()
    sub = ("" if scope.casefold() == (meta["fleet"] or "").strip().casefold()
           else f'<div class="sub">{html.escape(scope)}</div>')
    return (
        '<header class="mast">'
        + wordmark(meta["fleet"])
        + f'<div class="mid"><div class="ttl">{html.escape(title)}</div>{sub}</div>'
        + f'<div class="when"><div class="ttl">{span}</div>'
          f'<div class="sub">{n_days} day window · {html.escape(meta["tz"])} · '
          f'pulled {fmt_day(meta["pulled"])}</div></div></header>'
    )


def panel(title, note, body, cls="") -> str:
    return (f'<section class="panel {cls}"><div class="ph">'
            f'<h2>{html.escape(title)}</h2><p class="pn">{note}</p></div>'
            f'{body}</section>')


# ---------------------------------------------------------------- charting

def day_chart(daily, *, width=DAY_CHART_W, height=170) -> str:
    """Two bars a day: every submission, and the distinct trucks behind them.

    The SVG is emitted at exactly the size it occupies -- viewBox, width and
    height all agree -- so a font-size in here is the font-size on the page.
    An SVG stretched to fit a flexible column silently rescales its own labels,
    which is how chart text ends up smaller than everything around it.
    """
    n = max(len(daily), 1)
    pad_t, pad_b, pad_x = 18, 30, 2
    plot_h = height - pad_t - pad_b
    slot = (width - 2 * pad_x) / n
    top = max([d["n"] for d in daily] or [0]) or 1
    base = pad_t + plot_h

    gap = 2.4 if slot >= 26 else 1.0
    bw = max(1.6, min((slot * 0.7 - gap) / 2, 19))
    show_vals = slot >= 27
    every = 1 if slot >= 24 else max(1, round(26 / max(slot, 1)))
    weekdays = slot >= 22

    p = [f'<svg class="chart" width="{width}" height="{height}" '
         f'viewBox="0 0 {width} {height}" role="img" aria-label="'
         f'Submissions and distinct units per day">']

    for i, d in enumerate(daily):                       # weekends, behind all
        if d["day"].weekday() >= 5:
            p.append(f'<rect x="{pad_x + slot * i:.1f}" y="{pad_t - 5:.1f}" '
                     f'width="{slot:.1f}" height="{plot_h + 5:.1f}" class="wknd"/>')
    for gl in (1.0, 0.5):
        y = base - plot_h * gl
        p.append(f'<line x1="{pad_x}" y1="{y:.1f}" x2="{width - pad_x}" '
                 f'y2="{y:.1f}" class="grid"/>')
    p.append(f'<line x1="{pad_x}" y1="{base:.1f}" x2="{width - pad_x}" '
             f'y2="{base:.1f}" class="axis"/>')

    for i, d in enumerate(daily):
        cx = pad_x + slot * (i + 0.5)
        for off, key, cls in ((-bw - gap / 2, "n", "b-sub"),
                              (gap / 2, "units", "b-unit")):
            v, x = d[key], cx + off
            h = plot_h * v / top
            y = base - h if v else base - 1.4
            zero = "" if v else " zero"
            p.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" '
                     f'height="{max(h, 1.4):.1f}" rx="1.5" class="{cls}{zero}"/>')
            if show_vals and v:
                p.append(f'<text x="{x + bw / 2:.1f}" y="{y - 3.6:.1f}" '
                         f'class="vlab">{v}</text>')
        if show_vals and not d["n"]:
            p.append(f'<text x="{cx:.1f}" y="{base - 5:.1f}" class="vlab q">0</text>')
        if i % every == 0 or i == n - 1:
            p.append(f'<text x="{cx:.1f}" y="{height - (14 if weekdays else 9):.1f}" '
                     f'class="xlab">{d["day"].day:02d}</text>')
            if weekdays:
                dow = f'{d["day"]:%a}'.upper()
                p.append(f'<text x="{cx:.1f}" y="{height - 4:.1f}" '
                         f'class="xlab2">{dow}</text>')
    p.append("</svg>")
    return "".join(p)


QUALITY = (
    ("Complete", "q1", "every required area filmed"),
    ("Real", "q2", "1–2 areas unfilmed"),
    ("Partial", "q3", "3–5 areas unfilmed"),
    ("Not a PTI", "q4", "6 or more unfilmed"),
)


def quality_block(mix, total) -> str:
    segs, rows = [], []
    for name, cls, desc in QUALITY:
        v = mix.get(name, 0)
        share = 100 * v / total if total else 0
        if v:
            segs.append(f'<i class="{cls}" style="width:{share:.4f}%"></i>')
        rows.append(
            f'<div class="qrow"><span class="sw {cls}"></span>'
            f'<span class="qn">{html.escape(name)}</span>'
            f'<span class="qd">{html.escape(desc)}</span>'
            f'<span class="qv">{v}</span>'
            f'<span class="qp">{round(share)}%</span></div>')
    bar = "".join(segs) or '<i style="width:100%"></i>'
    return f'<div class="qbar">{bar}</div>{"".join(rows)}'


# --------------------------------------------------------------- rendering

def stats_html(agg, meta):
    t = agg["totals"]
    cov = round(100 * t["units_sent"] / t["units_active"]) if t["units_active"] else 0

    hero = (
        '<div class="hero"><div class="k">Fleet coverage</div>'
        f'<div class="v">{cov}%</div>'
        f'<div class="track {band(cov)}"><i style="width:{cov}%"></i></div>'
        f'<div class="d"><b>{t["units_sent"]} of {t["units_active"]}</b> '
        f'active units inspected<br><b>{t["silent"]}</b> silent · '
        f'{t["never_ever"]} never inspected at all</div></div>'
    )

    # Only the two 0-100 measures get a track; a count has no scale to sit on.
    avg_track = (f'<div class="track {band(t["avg"])}">'
                 f'<i style="width:{t["avg"]}%"></i></div>')
    # With nothing submitted the share of a share is not "0%", it is nothing at
    # all -- so the card keeps its definition and drops the percentage.
    n = t["inspections"]
    real_d = "at most two areas unfilmed"
    pass_d = "every required area filmed"
    if n:
        real_d = f'{pctf(t["real"], n)} of submissions — {real_d}'
        pass_d = f'{pctf(t["passed"], n)} — {pass_d}'
    cards = [
        ("Inspections", str(n), "",
         f'{t["drivers_sent"]} of {t["drivers_total"]} registered drivers'),
        ("Real walkarounds", str(t["real"]), "", real_d),
        ("Passed", str(t["passed"]), "", pass_d),
        ("Avg completeness", f'{t["avg"]}%', avg_track,
         "85 pts areas · 5 extinguisher · 10 detail"),
    ]
    card_html = "".join(
        f'<div class="card"><div class="k">{html.escape(k)}</div>'
        f'<div class="v">{v}</div>{extra}'
        f'<div class="d">{html.escape(d)}</div></div>'
        for k, v, extra, d in cards
    )

    chart_note = ('<span class="sw" style="background:var(--bar)"></span> '
                  'submissions <span class="sw" style="background:var(--bar-2)">'
                  '</span> distinct units')
    chart_panel = panel("Inspections per day", chart_note,
                        day_chart(agg["daily"], height=DAY_CHART_H), "p-day")

    mix_note = f'{t["inspections"]} submissions scored'
    mix_panel = panel("How complete they were", html.escape(mix_note),
                      quality_block(agg["klass_mix"], t["inspections"]), "p-mix")

    # --- active units that sent nothing: the action list
    shown = agg["silent"][:STATS_ROWS * 3]
    srows = []
    for u in shown:
        dim = "" if u["setup"] else ' class="dim"'
        last = fmt_day(u["last"]) if u["last"] else "never"
        srows.append(f'<tr{dim}><td class="u nw">{html.escape(str(u["unit"]))}</td>'
                     f'<td class="n">{u["drivers"]}</td>'
                     f'<td class="n">{last}</td></tr>')
    # Columns fill before they multiply, so a short list narrows instead of
    # spreading three rows across three columns -- but the three slots are
    # always laid out, or one column of units would stretch across the panel.
    n_cols = min(3, max(1, -(-len(srows) // STATS_ROWS)))
    per = max(-(-len(srows) // n_cols), 1)
    head_s = (colgroup(None, 26, 44)
              + '<thead><tr><th>Unit</th><th class="n">Drv</th>'
                '<th class="n">Last PTI</th></tr></thead>')
    chunks = [srows[i:i + per] for i in range(0, len(srows), per)]
    silent_body = "".join(
        f'<div><table>{head_s}<tbody>{"".join(c)}</tbody></table></div>'
        for c in chunks
    ) + "<div></div>" * (3 - len(chunks))
    if not srows:
        silent_body = '<p class="d">Every active unit submitted at least once.</p>'
    more = len(agg["silent"]) - len(shown)
    silent_note = (f'{t["never_ever"]} never inspected'
                   + (f' · {more} more in the CSV' if more > 0 else ''))
    silent_panel = panel(f'Silent units — {t["silent"]} of {t["units_active"]}',
                         html.escape(silent_note),
                         f'<div class="cols">{silent_body}</div>', "p-silent")

    # --- who did submit
    submitters = [r for r in agg["driver_rows"] if r["submissions"]]
    top = submitters[:STATS_ROWS]
    trows = "".join(
        f'<tr><td class="u">{html.escape(r["name"][:28])}</td>'
        f'<td class="nw">{html.escape(str(r["unit"]))}</td>'
        f'<td class="n">{r["submissions"]}</td>'
        f'<td class="n">{r["real"]}</td><td class="n">{r["passed"]}</td>'
        f'<td class="n">{pct_cell(r["avg"])}</td></tr>'
        for r in top
    )
    rest = len(submitters) - len(top)
    drv_note = (f'top {len(top)} of {len(submitters)} · the rest in the driver report'
                if rest > 0 else 'full detail in the driver report')
    drv_body = (
        '<table>' + colgroup(None, 66, 30, 34, 36, 76)
        + '<thead><tr><th>Driver</th><th>Unit</th><th class="n">Sub</th>'
          '<th class="n">Real</th><th class="n">Pass</th>'
          '<th class="b">Avg</th></tr></thead>'
        f'<tbody>{trows}</tbody></table>'
    ) if top else '<p class="d">Nobody submitted an inspection in this window.</p>'
    drv_panel = panel("Drivers who submitted", html.escape(drv_note), drv_body,
                      "p-drv")

    last_day = fmt_day(meta["until"] - timedelta(days=1))
    legend = (
        '<div class="legend">'
        '<div><span class="lt">Real PTI</span>Read off the area coverage the '
        'inspection recorded, not its verdict — coverage is the one thing an '
        'unrelated clip cannot fake.</div>'
        '<div><span class="lt">Pass is stricter</span>Every required area filmed. '
        'The extinguisher is scored but never fails one, so filming all else '
        'scores 95% and passes.</div>'
        '<div><span class="lt">Units, not drivers</span>Two drivers share a truck '
        'and often only one uses the app, so a silent <em>unit</em> is the real '
        'gap. Greyed units have unfinished setup.</div>'
        f'<div><span class="lt">The window</span>Days are '
        f'{html.escape(meta["tz"])}; the range is half-open, so {last_day} is the '
        f'last day counted. Unrounded numbers are in the CSVs.</div>'
        '</div>'
    )

    return (
        _page_head(meta["title"], "letter landscape", "11mm 12mm")
        + masthead(meta, "Fleet inspection statistics")
        + f'<div class="kpis">{hero}{card_html}</div>'
        + f'<div class="row">{chart_panel}{mix_panel}</div>'
        + f'<div class="row">{silent_panel}{drv_panel}</div>'
        + legend
        + '</body></html>'
    )


def driver_html(agg, meta):
    t = agg["totals"]
    rows = agg["driver_rows"]

    idx = []
    for i, r in enumerate(rows, 1):
        anchor = f'd{r["key"][0]}_{r["key"][1]}'
        dim = "" if r["submissions"] else ' class="dim"'
        avg = pct_cell(r["avg"]) if r["submissions"] else "—"
        idx.append(
            f'<tr{dim}><td class="n">{i}</td>'
            f'<td class="u"><a href="#{anchor}">{html.escape(r["name"][:26])}</a></td>'
            f'<td class="nw">{html.escape(str(r["unit"]))}</td>'
            f'<td class="n">{r["real"]}</td><td class="n">{r["passed"]}</td>'
            f'<td class="n">{avg}</td></tr>')
    head = (colgroup(26, None, 52, 30, 32, 72)
            + '<thead><tr><th class="n">#</th><th>Driver</th><th>Unit</th>'
              '<th class="n">Real</th><th class="n">Pass</th>'
              '<th class="b">Avg</th></tr></thead>')
    # One block per page, forced. How many rows actually fit depends on how
    # many names wrap to a second line, so a block sized to the page exactly
    # would sometimes spill two rows onto the next one -- which then carries a
    # stranded stub above the block that belongs there. Undersized blocks plus
    # a hard break leave clean white space at the foot of a page instead. Page
    # one is the short block: it also carries the masthead and the primer.
    first_rows, page_rows = 28, 38
    chunks, i = [], 0
    while i < len(idx):
        per_col = first_rows if not chunks else page_rows
        chunks.append(idx[i:i + per_col * 2])
        i += per_col * 2
    # Chromium cannot number printed pages from CSS, so each index page names
    # the slice of the ranking it carries instead -- which is the thing a
    # reader actually wants from a page number here.
    blocks, lo = [], 1
    for k, take in enumerate(chunks):
        per_col = -(-len(take) // 2)
        duo = [take[:per_col], take[per_col:]]
        if len(chunks) == 1:
            title = (f'All drivers — ranked by real PTIs · {t["drivers_sent"]} '
                     f'of {t["drivers_total"]} submitted')
        elif k == 0:
            title = (f'All drivers — ranked by real PTIs · 1–{len(take)} of '
                     f'{len(idx)}')
        else:
            title = (f'All drivers, continued · {lo}–{lo + len(take) - 1} of '
                     f'{len(idx)}')
        pg = "" if k == len(chunks) - 1 else " pg"
        blocks.append(
            f'<h2 class="sec">{html.escape(title)}</h2>'
            f'<div class="cols{pg}">' + "".join(
                f'<div><table>{head}<tbody>{"".join(c)}</tbody></table></div>'
                for c in duo if c) + '</div>')
        lo += len(take)

    sections = []
    for r in rows:
        if not r["items"]:
            continue
        anchor = f'd{r["key"][0]}_{r["key"][1]}'
        lines = []
        for it in r["items"]:
            s = it["score"]
            chips = "".join(f'<span class="chip hot">{html.escape(a)}</span>'
                            for a in s.missing)
            notes = []
            if not s.fire_extinguisher:
                notes.append("extinguisher not shown")
            notes += [f"{n} not visible" for n in s.not_visible]
            note = (f'<span class="nv">{html.escape(" · ".join(notes))}</span>'
                    if notes else "")
            if not chips and not note:
                chips = '<span class="ok">nothing missing</span>'
            verdict = ('<span class="badge p">PASS</span>' if it["passed"]
                       else '<span class="badge f">FAIL</span>')
            lines.append(
                f'<tr><td class="nw">{fmt_day(it["day"])}</td>'
                f'<td class="n">{pct_cell(s.score)}</td>'
                f'<td>{s.klass}</td>'
                f'<td class="n">{s.filmed}/{s.required}</td>'
                f'<td>{verdict}</td>'
                f'<td>{chips}{" " if chips and note else ""}{note}</td></tr>')
        stats = (f'<em>{r["submissions"]}</em> subs<span><em>{r["real"]}</em> real'
                 f'</span><span><em>{r["passed"]}</em> passed</span>'
                 f'<span><em>{r["avg"]}%</em> avg{sbar(r["avg"])}</span>'
                 f'<span><em>{r["best"]}%</em> best</span>')
        sections.append(
            f'<section class="drv" id="{anchor}"><div class="dh">'
            f'<span class="dn">{html.escape(r["name"])}</span>'
            f'<span class="du">unit {html.escape(str(r["unit"]))}</span>'
            f'<span class="st">{stats}</span></div>'
            '<table>' + colgroup(56, 84, 66, 44, 56, None)
            + '<thead><tr><th>Date</th><th class="b">Score</th><th>Class</th>'
              '<th class="n">Areas</th><th>Verdict</th>'
              '<th>Areas not filmed</th></tr></thead>'
            f'<tbody>{"".join(lines)}</tbody></table></section>')

    silent_drivers = sum(1 for r in rows if not r["submissions"])
    last_day = fmt_day(meta["until"] - timedelta(days=1))
    intro = (
        '<div class="intro">'
        '<div><span class="lt">The completeness score</span>85 pts for the required '
        'areas actually filmed (8, or 9 once the under-hood check appears in the '
        'footage), 5 pts for showing the fire extinguisher, and 10 pts when no '
        'sub-item was flagged "not visible" — 2 off for each that was.</div>'
        '<div><span class="lt">The score is not the verdict</span>PASS/FAIL is '
        'decided only by whether every required area was filmed. The extinguisher '
        'never fails an inspection, so filming everything but the extinguisher '
        'scores 95% and still passes.</div>'
        f'<div><span class="lt">Reading the list</span>Class is by areas unfilmed: '
        f'Complete 0, Real 1–2, Partial 3–5, Not a PTI 6+. The '
        f'{silent_drivers} greyed drivers submitted nothing in this window and show '
        f'"—" rather than 0%. Days are {html.escape(meta["tz"])}; {last_day} is the '
        f'last day counted.</div>'
        '</div>'
    )

    return (
        _page_head(meta["title_d"], "letter portrait", "12mm")
        + masthead(meta, "Driver inspection report")
        + intro
        + "".join(blocks)
        + f'<h2 class="sec">Inspections by driver · {t["inspections"]} in the '
          f'window</h2>'
        + "".join(sections)
        + '</body></html>'
    )


# ------------------------------------------------------------------ output

def to_pdf(html_text: str, out: Path) -> None:
    chrome = next((c for c in CHROME_CANDIDATES if os.path.exists(c)), None)
    if not chrome:
        raise SystemExit("No Chromium binary found; cannot render PDF.")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "r.html"
        src.write_text(html_text, encoding="utf-8")
        subprocess.run(
            [chrome, "--headless", "--no-sandbox", "--disable-gpu",
             "--no-pdf-header-footer", f"--print-to-pdf={out}", str(src)],
            check=True, capture_output=True, timeout=180,
        )


def write_csvs(agg, stem: Path) -> list[Path]:
    made = []
    p = stem.with_name(stem.name + "-drivers.csv")
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["driver", "unit", "submissions", "real_ptis", "passed",
                    "avg_completeness", "best"])
        for r in agg["driver_rows"]:
            w.writerow([r["name"], r["unit"], r["submissions"], r["real"],
                        r["passed"], r["avg"], r["best"]])
    made.append(p)

    p = stem.with_name(stem.name + "-silent-units.csv")
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["unit", "drivers", "setup_complete", "ptis_ever", "last_pti"])
        for u in agg["silent"]:
            w.writerow([u["unit"], u["drivers"], u["setup"], u["ever"],
                        u["last"].isoformat() if u["last"] else ""])
    made.append(p)

    p = stem.with_name(stem.name + "-inspections.csv")
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["day", "unit", "driver", "score", "class", "filmed",
                    "required", "verdict", "areas_not_filmed",
                    "fire_extinguisher_shown", "not_visible"])
        for i in agg["inspections"]:
            s = i["score"]
            w.writerow([i["day"].isoformat(), i["unit"], i["name"], s.score,
                        s.klass, s.filmed, s.required,
                        "PASS" if i["passed"] else "FAIL", "; ".join(s.missing),
                        s.fire_extinguisher, "; ".join(s.not_visible)])
    made.append(p)
    return made


# -------------------------------------------------------------------- main

def resolve_window(args, tz: ZoneInfo) -> tuple[date, date]:
    today = datetime.now(tz).date()
    if args.since:
        since = date.fromisoformat(args.since)
        until = date.fromisoformat(args.until) if args.until else today + timedelta(days=1)
        return since, until
    if args.last_week:
        # The most recently *completed* Monday-Sunday week.
        this_monday = today - timedelta(days=today.weekday())
        return this_monday - timedelta(days=7), this_monday
    return today - timedelta(days=args.days) + timedelta(days=1), today + timedelta(days=1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fleet", required=True, help='e.g. "jrd-pti"')
    ap.add_argument("--scope", default=None, help='defaults to "<fleet> / production"')
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--tz", default=os.environ.get("FLEET_TZ", "America/New_York"))
    ap.add_argument("--last-week", action="store_true",
                    help="the most recently completed Monday-Sunday week")
    ap.add_argument("--days", type=int, default=7,
                    help="rolling window ending today (default 7)")
    ap.add_argument("--since", help="YYYY-MM-DD, inclusive")
    ap.add_argument("--until", help="YYYY-MM-DD, exclusive")
    ap.add_argument("--out", default="reports")
    args = ap.parse_args()

    if not args.database_url:
        ap.error("no database: pass --database-url or set DATABASE_URL")

    tz = ZoneInfo(args.tz)
    since, until = resolve_window(args, tz)
    if until <= since:
        ap.error(f"empty window: {since} .. {until}")

    since_utc = datetime.combine(since, datetime.min.time(), tz).astimezone(
        timezone.utc).replace(tzinfo=None)
    until_utc = datetime.combine(until, datetime.min.time(), tz).astimezone(
        timezone.utc).replace(tzinfo=None)

    data = asyncio.run(fetch(args.database_url, since_utc, until_utc))
    agg = build(data, tz, since, until)

    meta = {
        "fleet": display_name(args.fleet),
        "scope": args.scope or f"{args.fleet} / production",
        "since": since, "until": until,
        "pulled": datetime.now(tz).date(),
        "tz": args.tz,
        "title": f"{args.fleet} fleet inspection statistics",
        "title_d": f"{args.fleet} driver inspection report",
    }

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tag = f"{args.fleet}-{until - timedelta(days=1):%Y%m%d}"
    stats_pdf = out / f"{tag}.pdf"
    driver_pdf = out / f"{tag}-driver-report.pdf"

    to_pdf(stats_html(agg, meta), stats_pdf)
    to_pdf(driver_html(agg, meta), driver_pdf)
    csvs = write_csvs(agg, out / tag)

    t = agg["totals"]
    print(f"{since} .. {until - timedelta(days=1)}  ({args.tz})")
    print(f"  {t['inspections']} inspections · {t['real']} real · {t['passed']} passed "
          f"· avg {t['avg']}%")
    print(f"  {t['units_sent']}/{t['units_active']} active units submitted "
          f"· {t['silent']} silent ({t['never_ever']} never sent one at all)")
    print(f"  {t['drivers_sent']}/{t['drivers_total']} drivers submitted")
    for p in (stats_pdf, driver_pdf, *csvs):
        print(f"  wrote {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
