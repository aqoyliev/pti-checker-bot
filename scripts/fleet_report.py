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
import csv
import html
import importlib.util
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
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
        s = score_inspection(row["result_json"])
        gid = row["group_id"]
        g = groups.get(gid, {})
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
            "drivers_total": len(driver_name),
            "drivers_sent": len({(i["group_id"], i["user_id"]) for i in inspections}),
            "units_total": len(groups),
            "units_active": len(active_groups),
            "units_sent": len(units_with),
            "silent": len(silent),
            "never_ever": len(never_ever),
        },
    }


# ---------------------------------------------------------------- charting

def bar_chart(series, value_key, title, note, *, width=470, height=150,
              label_every=1):
    """A labelled bar chart. Every plotted value is printed, so nothing here
    depends on colour alone -- the reports are read in greyscale on phones."""
    vals = [s[value_key] for s in series] or [0]
    top = max(max(vals), 1)
    n = len(series)
    pad_l, pad_b, pad_t = 4, 18, 16
    plot_h = height - pad_b - pad_t
    slot = (width - pad_l) / max(n, 1)
    bw = max(2.0, min(slot * 0.68, 26))

    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" '
             f'role="img" aria-label="{html.escape(title)}">']
    for gl in (0, 0.5, 1.0):
        y = pad_t + plot_h - plot_h * gl
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width}" y2="{y:.1f}" '
                     f'class="grid"/>')
    for idx, s in enumerate(series):
        v = s[value_key]
        h = plot_h * v / top
        x = pad_l + slot * idx + (slot - bw) / 2
        y = pad_t + plot_h - h
        cls = "bar zero" if v == 0 else "bar"
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" '
                     f'height="{max(h, 0.6):.1f}" class="{cls}"/>')
        if v:
            parts.append(f'<text x="{x + bw / 2:.1f}" y="{y - 3:.1f}" '
                         f'class="vlab">{v}</text>')
        if idx % label_every == 0 or idx == n - 1:
            parts.append(f'<text x="{x + bw / 2:.1f}" y="{height - 5}" '
                         f'class="xlab">{html.escape(s["label"])}</text>')
    parts.append("</svg>")
    return (f'<div class="chartbox"><h3>{html.escape(title)}</h3>'
            f'<p class="note">{html.escape(note)}</p>{"".join(parts)}</div>')


# --------------------------------------------------------------- rendering

CSS = """
@page { size: __PAGE__; margin: __MARGIN__; }
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", Inter, Helvetica, Arial, sans-serif;
       color: #16181d; margin: 0; font-size: 9.2px; line-height: 1.35;
       -webkit-print-color-adjust: exact; print-color-adjust: exact; }
h1 { font-size: 15px; font-weight: 620; margin: 0; letter-spacing: -0.15px; }
h2 { font-size: 10.5px; font-weight: 620; margin: 13px 0 5px;
     padding-bottom: 3px; border-bottom: 1px solid #d9dde3; }
h3 { font-size: 9.4px; font-weight: 620; margin: 0 0 1px; }
.sub { color: #6b7280; font-size: 8.6px; margin: 2px 0 0; }
.note { color: #6b7280; font-size: 7.9px; margin: 0 0 3px; }
.cards { display: flex; gap: 7px; margin: 10px 0 4px; }
.card { flex: 1; border: 1px solid #dfe3e9; border-radius: 5px; padding: 6px 8px; }
.card .k { font-size: 7.6px; text-transform: uppercase; letter-spacing: .35px;
           color: #6b7280; }
.card .v { font-size: 19px; font-weight: 640; letter-spacing: -0.5px;
           margin: 1px 0 0; }
.card .d { font-size: 7.8px; color: #6b7280; }
.charts { display: flex; gap: 12px; margin-top: 8px; }
.chartbox { flex: 1; min-width: 0; }
.chart { width: 100%; height: auto; display: block; }
.bar { fill: #2f6f4f; }
.bar.zero { fill: #d8dce2; }
.grid { stroke: #e8ebef; stroke-width: .6; }
.vlab { font-size: 6.6px; fill: #5b6472; text-anchor: middle; font-weight: 600; }
.xlab { font-size: 6.2px; fill: #6b7280; text-anchor: middle; }
table { border-collapse: collapse; width: 100%; }
th { font-size: 7.4px; text-transform: uppercase; letter-spacing: .35px;
     color: #6b7280; font-weight: 600; text-align: left;
     border-bottom: 1px solid #d9dde3; padding: 2px 4px; }
td { padding: 1.7px 4px; border-bottom: 1px solid #f1f3f5; vertical-align: top; }
td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; }
tr.muted td { color: #98a0ac; }
.cols { display: flex; gap: 12px; align-items: flex-start; }
.cols > * { flex: 1; min-width: 0; }
.idx table { max-width: 300px; }
.foot { margin-top: 9px; font-size: 7.6px; color: #6b7280; line-height: 1.5; }
.foot b { color: #374151; font-weight: 600; }
a { color: inherit; text-decoration: none; }
.pill { font-size: 7.2px; padding: 0 3px; border-radius: 3px; background: #eef1f4;
        color: #4b5563; }
.dhead { margin-top: 11px; page-break-after: avoid; break-after: avoid; }
.dhead .nm { font-weight: 640; font-size: 10px; }
.dhead .mt { color: #6b7280; font-size: 8.2px; }
section { page-break-inside: auto; }
tr { page-break-inside: avoid; }
"""


def _page_head(title, page, margin):
    # str.replace, not %-formatting: the stylesheet is full of literal "%".
    css = CSS.replace("__PAGE__", page).replace("__MARGIN__", margin)
    return (f'<!doctype html><html><head><meta charset="utf-8">'
            f'<title>{html.escape(title)}</title>'
            f'<style>{css}</style></head><body>')


def fmt_day(d: date) -> str:
    return f"{d.day:02d} {d:%b}"


def display_name(fleet: str) -> str:
    """"jrd-pti" -> "jrd". The headline already says "pti"."""
    low = fleet.lower()
    for suffix in ("-pti", "_pti", " pti"):
        if low.endswith(suffix):
            return fleet[: -len(suffix)]
    return fleet


def stats_html(agg, meta):
    t = agg["totals"]
    daily = agg["daily"]
    n_days = len(daily)
    label_every = 1 if n_days <= 10 else max(1, n_days // 10)
    for d in daily:
        d["label"] = fmt_day(d["day"])

    pct = lambda a, b: f"{round(100 * a / b)}%" if b else "0%"  # noqa: E731

    cards = [
        ("Inspections", t["inspections"],
         f"{t['real']} real walkarounds ({pct(t['real'], t['inspections'])})"),
        ("Passed", t["passed"], f"{pct(t['passed'], t['inspections'])} of submissions"),
        ("Units that submitted", t["units_sent"],
         f"of {t['units_active']} active · {t['silent']} sent nothing"),
        ("Drivers who submitted", t["drivers_sent"],
         f"of {t['drivers_total']} on the roster"),
        ("Average completeness", f"{t['avg']}%",
         "85 areas · 5 extinguisher · 10 detail"),
    ]
    card_html = "".join(
        f'<div class="card"><div class="k">{html.escape(k)}</div>'
        f'<div class="v">{v}</div><div class="d">{html.escape(d)}</div></div>'
        for k, v, d in cards
    )

    charts = [
        bar_chart(daily, "n", "Submissions per day",
                  f"Every day in the window. {sum(d['n'] for d in daily)} in total.",
                  label_every=label_every),
        bar_chart(daily, "units", "Units submitting each day",
                  "Distinct trucks with at least one inspection that day.",
                  label_every=label_every),
    ]
    mix_order = ["Complete", "Real", "Partial", "Not a PTI"]
    mix = [{"label": k, "n": agg["klass_mix"].get(k, 0)} for k in mix_order]
    charts.append(bar_chart(
        mix, "n", "How complete the inspections were",
        "Complete = every required area filmed. Real = 1–2 unfilmed."))

    # --- silent units
    srows = []
    for u in agg["silent"][:44]:
        cls = "" if u["setup"] else ' class="muted"'
        last = fmt_day(u["last"]) if u["last"] else "never"
        srows.append(
            f'<tr{cls}><td>{html.escape(str(u["unit"]))}</td>'
            f'<td class="n">{u["drivers"]}</td><td>{last}</td></tr>')
    chunk = max((len(srows) + 2) // 3, 12)
    silent_cols = "".join(
        '<div><table><thead><tr><th>Unit</th><th class="n">Drv</th>'
        '<th>Last PTI</th></tr></thead><tbody>'
        + "".join(srows[i:i + chunk]) + "</tbody></table></div>"
        for i in range(0, len(srows), chunk)
    ) or '<div class="note">Every active unit submitted at least once.</div>'

    # --- top drivers
    top = [r for r in agg["driver_rows"] if r["submissions"]][:24]
    trows = [
        f'<tr><td>{html.escape(r["name"][:22])}</td>'
        f'<td>{html.escape(str(r["unit"]))}</td>'
        f'<td class="n">{r["real"]}</td><td class="n">{r["passed"]}</td>'
        f'<td class="n">{r["avg"]}%</td></tr>' for r in top
    ]
    chunk = max((len(trows) + 2) // 3, 8)
    top_cols = "".join(
        '<div><table><thead><tr><th>Driver</th><th>Unit</th><th class="n">Real</th>'
        '<th class="n">Pass</th><th class="n">Avg</th></tr></thead><tbody>'
        + "".join(trows[i:i + chunk]) + "</tbody></table></div>"
        for i in range(0, len(trows), chunk)
    ) or '<div class="note">No inspections in this window.</div>'

    foot = (
        f'<b>Real PTIs</b> — from the per-submission area coverage Gemini recorded '
        f'(missing_areas): 0 = complete, 1–2 = real walkaround, 3–5 = partial, '
        f'6+ = not a PTI. Coverage cannot be faked with an unrelated clip.  ·  '
        f'<b>Pass</b> is the stricter bar: every required area filmed. The fire '
        f'extinguisher never fails an inspection.  ·  <b>Why units, not drivers</b> — '
        f'two drivers share a truck and often only one uses the app, so a silent '
        f'unit is the real gap, not a silent driver.  ·  '
        f'<b>{t["never_ever"]} of the {t["silent"]} silent units have never sent a '
        f'PTI at all</b>, in any window.  ·  Days are '
        f'{html.escape(meta["tz"])}; the window is half-open, so '
        f'{fmt_day(meta["until"] - timedelta(days=1))} is the last day counted.  ·  '
        f'Every plotted value is labelled, so nothing depends on colour alone.'
    )

    return (
        _page_head(meta["title"], "letter landscape", "11mm 12mm")
        + f'<h1>{html.escape(meta["fleet"])} pti fleet inspection statistics</h1>'
        + f'<p class="sub">{html.escape(meta["scope"])} · '
          f'{fmt_day(meta["since"])} – {fmt_day(meta["until"] - timedelta(days=1))} '
          f'{meta["until"].year} · pulled {fmt_day(meta["pulled"])}</p>'
        + f'<div class="cards">{card_html}</div>'
        + f'<div class="charts">{"".join(charts)}</div>'
        + f'<h2>Active units that sent no PTI — {t["silent"]} of {t["units_active"]}</h2>'
        + '<p class="note">Greyed rows are unfinished setup. "Last PTI" is the most '
          'recent inspection ever recorded for that truck, in any window.</p>'
        + f'<div class="cols">{silent_cols}</div>'
        + '<h2>Drivers who submitted — ranked by real PTIs</h2>'
        + f'<div class="cols">{top_cols}</div>'
        + f'<div class="foot">{foot}</div></body></html>'
    )


def driver_html(agg, meta):
    t = agg["totals"]
    rows = agg["driver_rows"]

    idx = []
    for i, r in enumerate(rows, 1):
        anchor = f'd{r["key"][0]}_{r["key"][1]}'
        cls = "" if r["submissions"] else ' class="muted"'
        idx.append(
            f'<tr{cls}><td class="n">{i}</td>'
            f'<td><a href="#{anchor}">{html.escape(r["name"][:20])}</a></td>'
            f'<td>{html.escape(str(r["unit"]))}</td>'
            f'<td class="n">{r["real"]}</td><td class="n">{r["passed"]}</td>'
            f'<td class="n">{str(r["avg"]) + "%" if r["submissions"] else "—"}'
            f'</td></tr>')
    per_col = 48
    head = ('<thead><tr><th class="n">#</th><th>Driver</th><th>Unit</th>'
            '<th class="n">Real</th><th class="n">Pass</th>'
            '<th class="n">Avg</th></tr></thead>')
    blocks, i = [], 0
    while i < len(idx):
        trio = [idx[i + k * per_col:i + (k + 1) * per_col] for k in range(3)]
        blocks.append('<div class="cols idx">' + "".join(
            f'<div><table>{head}<tbody>{"".join(c)}</tbody></table></div>'
            for c in trio if c) + "</div>")
        i += per_col * 3

    sections = []
    for r in rows:
        if not r["items"]:
            continue
        anchor = f'd{r["key"][0]}_{r["key"][1]}'
        lines = []
        for it in r["items"]:
            s = it["score"]
            notes = []
            if not s.fire_extinguisher:
                notes.append("fire extinguisher not shown")
            notes += s.not_visible
            lines.append(
                f'<tr><td>{fmt_day(it["day"])}</td>'
                f'<td class="n">{s.score}%</td><td>{s.klass}</td>'
                f'<td class="n">{s.filmed}/{s.required}</td>'
                f'<td>{"PASS" if it["passed"] else "FAIL"}</td>'
                f'<td>{html.escape(", ".join(s.missing) or "—")}'
                f'{" · " + html.escape("; ".join(notes)) if notes else ""}</td></tr>')
        sections.append(
            f'<section id="{anchor}"><div class="dhead">'
            f'<span class="nm">{html.escape(r["name"])}</span> '
            f'<span class="mt">unit {html.escape(str(r["unit"]))} · '
            f'{r["real"]} real PTIs of {r["submissions"]} submissions · '
            f'{r["passed"]} passed · average completeness {r["avg"]}% · '
            f'best {r["best"]}%</span></div>'
            f'<table><thead><tr><th>Date</th><th class="n">Score</th><th>Class</th>'
            f'<th class="n">Areas</th><th>Verdict</th><th>Areas not filmed</th>'
            f'</tr></thead><tbody>{"".join(lines)}</tbody></table></section>')

    return (
        _page_head(meta["title_d"], "letter portrait", "12mm")
        + f'<h1>{html.escape(meta["fleet"])} pti — driver inspection report</h1>'
        + f'<p class="sub">{html.escape(meta["scope"])} · '
          f'{fmt_day(meta["since"])} – {fmt_day(meta["until"] - timedelta(days=1))} '
          f'{meta["until"].year} · {t["inspections"]} inspections by '
          f'{t["drivers_sent"]} drivers · pulled {fmt_day(meta["pulled"])}</p>'
        + '<p class="note"><b>Completeness score</b> — 85 pts for required areas '
          'actually filmed (8, or 9 when the under-hood check applies), 5 pts if the '
          'fire extinguisher was shown, 10 pts if no specific sub-item was flagged '
          '"not visible" (−2 each). So a driver who filmed every area but never '
          'showed the extinguisher scores 95%, and the verdict is still whatever the '
          'areas gave.</p>'
        + '<p class="note"><b>Important</b> — the bot\'s PASS/FAIL is decided only by '
          'whether every required area was filmed; the fire extinguisher never fails '
          'an inspection. <b>Class</b> — Complete = 0 areas unfilmed, Real = 1–2, '
          'Partial = 3–5, Not a PTI = 6+. Greyed drivers submitted nothing in this '
          'window.</p>'
        + '<h2>All drivers — ranked by real PTIs</h2>'
        + "".join(blocks)
        + '<h2>Inspections by driver</h2>'
        + "".join(sections)
        + "</body></html>"
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
