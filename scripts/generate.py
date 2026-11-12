#!/usr/bin/env python3
"""Render the static status page (site/index.html) from the history files.

Reads data/daily/*.json (last 90 days) and data/yearly/*.json (all-time
aggregates) and fills assets/template.html. Python stdlib only.

Usage:
    python3 scripts/generate.py [--data-dir data] [--out site]
"""

import argparse
import html
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent / "assets" / "template.html"
STATUS_CATEGORIES = ("infrastructure", "workloads")
SEVERITY = {"ok": 0, "warning": 1, "error": 2}
WINDOWS = [("24h", 1), ("7d", 7), ("30d", 30), ("90d", 90)]
DAYS_SHOWN = 90
MAX_INCIDENTS = 30

LEGACY_CATEGORIES = {
    "private": "infrastructure",
    "public": "workloads",
    "customers": "workloads",
    "unknown": "workloads",
}


def normalize_categories(categories: dict) -> dict:
    """Normalize stored category counts to the current names.

    Legacy history entries use private / public / customers / unknown;
    current ones use infrastructure / workloads. Everything legacy lands in
    infrastructure or workloads — there is no separate unknown bucket.
    """
    counts = {"infrastructure": {}, "workloads": {}}
    for cat, value in (categories or {}).items():
        counts[LEGACY_CATEGORIES.get(cat, cat if cat in counts else "workloads")] = dict(value)
    for legacy in ("public", "customers", "unknown"):
        if legacy in (categories or {}):
            for key, value in categories[legacy].items():
                counts["workloads"][key] = counts["workloads"].get(key, 0) + int(value)
    return {
        cat: {
            "ok": int(counts[cat].get("ok", 0)),
            "warning": int(counts[cat].get("warning", 0)),
            "error": int(counts[cat].get("error", 0)),
        }
        for cat in STATUS_CATEGORIES
    }


def parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def load_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        return default


def load_daily(data_dir: Path) -> list[dict]:
    daily_dir = data_dir / "daily"
    if not daily_dir.is_dir():
        return []
    entries = []
    for path in sorted(daily_dir.glob("*.json")):
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"warn: skipping {path}: {exc}", file=sys.stderr)
            continue
        for entry in loaded:
            for result in entry.get("hosts", {}).values():
                if isinstance(result.get("categories"), dict):
                    result["categories"] = normalize_categories(result["categories"])
        entries.extend(loaded)
    return entries


def load_yearly(data_dir: Path) -> list[dict]:
    yearly_dir = data_dir / "yearly"
    if not yearly_dir.is_dir():
        return []
    days = []
    for path in sorted(yearly_dir.glob("*.json")):
        try:
            days.extend(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"warn: skipping {path}: {exc}", file=sys.stderr)
    return days


def worst(statuses: list[str]) -> str:
    return max(statuses, key=lambda s: SEVERITY.get(s, 2), default="ok")


def uptime(entries: list[dict], host: str, since: datetime | None = None) -> tuple[float | None, int]:
    """Return (uptime %, total runs) for one host, optionally since a time."""
    total = ok = 0
    for entry in entries:
        result = entry.get("hosts", {}).get(host)
        if result is None:
            continue
        if since and parse_ts(entry["timestamp"]) < since:
            continue
        total += 1
        if result.get("status") != "error":
            ok += 1
    return (round(100 * ok / total, 2) if total else None), total


def badge(status: str) -> str:
    return f'<span class="badge {status}">{status}</span>'


def category_severity(c: dict) -> str:
    """Severity label for one category: error if any error, warning if any
    warning, else ok."""
    if c.get("error", 0) > 0:
        return "error"
    if c.get("warning", 0) > 0:
        return "warning"
    return "ok"


def category_grid(categories: dict | None) -> str:
    if not categories:
        return '<p class="muted">no data</p>'
    cells = []
    for cat in STATUS_CATEGORIES:
        c = categories.get(cat) or {}
        sev = category_severity(c)
        cells.append(
            f'<div class="cat {sev}"><span class="cat-name">{html.escape(cat)}</span>'
            f'<span class="cat-sev">{sev}</span></div>'
        )
    return f'<div class="cats">{"".join(cells)}</div>'


def day_strip(entries: list[dict], host: str, today: datetime) -> str:
    per_day: dict[str, list[str]] = {}
    for entry in entries:
        result = entry.get("hosts", {}).get(host)
        if result:
            per_day.setdefault(entry["timestamp"][:10], []).append(result.get("status", "error"))

    bars = []
    for offset in range(DAYS_SHOWN - 1, -1, -1):
        day = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        statuses = per_day.get(day)
        if statuses:
            level = worst(statuses)
            ok_count = sum(1 for s in statuses if s != "error")
            title = f"{day}: {ok_count}/{len(statuses)} runs up, worst {level}"
        else:
            level, title = "nodata", f"{day}: no data"
        bars.append(f'<div class="day {level}" title="{html.escape(title)}"></div>')
    return f'<div class="strip">{"".join(bars)}</div>'


def latency_graph(entries: list[dict], host: str, now: datetime) -> str:
    """SVG sparkline: average response time per hour over the last 24 hours.

    Under the line, one small thick tick per hour shows the worst response
    status of that hour (green/yellow/red), aligned with the line's x axis.
    Hours without runs leave a gap. Scaled per host.
    """
    W, H = 240.0, 44.0
    PAD, LINE_BOTTOM = 4.0, 30.0
    TICK_H, TICK_W = 6.0, 8.0
    step = W / 24.0

    per_hour: dict[str, list[tuple]] = {}
    for entry in entries:
        result = entry.get("hosts", {}).get(host)
        if result:
            per_hour.setdefault(entry["timestamp"][:13], []).append(
                (result.get("latency_ms"), result.get("status", "nodata")))

    points, ticks = [], []
    for h in range(23, -1, -1):
        hour = now - timedelta(hours=h)
        runs = per_hour.get(hour.strftime("%Y-%m-%dT%H"), [])
        idx = 23 - h
        lats = [lat for lat, _ in runs if lat is not None]
        if lats:
            points.append((idx, sum(lats) / len(lats)))
        if runs:
            ticks.append((idx, worst([s for _, s in runs])))

    if not points and not ticks:
        return '<div class="latency-graph"><span class="empty">no response time data</span></div>'

    top = max((v for _, v in points), default=0.0) * 1.15 or 1.0

    def x(i: int) -> float:
        return (i + 0.5) * step

    def y(v: float) -> float:
        return PAD + (1 - v / top) * (LINE_BOTTOM - PAD)

    parts = []
    prev_i = None
    for i, v in points:
        cmd = "M" if prev_i is None or i != prev_i + 1 else "L"
        parts.append(f"{cmd}{x(i):.1f},{y(v):.1f}")
        prev_i = i
    path = " ".join(parts)

    tick_colors = {"ok": "var(--ok)", "warning": "var(--warning)",
                   "error": "var(--error)", "nodata": "var(--nodata)"}
    rects = ""
    for i, s in ticks:
        hour = now - timedelta(hours=23 - i)
        hour_runs = per_hour.get(hour.strftime("%Y-%m-%dT%H"), [])
        hour_lats = [lat for lat, _ in hour_runs if lat is not None]
        tip = hour.strftime("%b %d, %H:00 UTC") + " — " + s
        if hour_lats:
            tip += f", avg {int(round(sum(hour_lats) / len(hour_lats)))} ms"
        rects += (
            f'<rect x="{x(i) - TICK_W / 2:.1f}" y="{H - TICK_H - 1:.1f}" '
            f'width="{TICK_W:.0f}" height="{TICK_H:.0f}" rx="1" fill="{tick_colors[s]}">'
            f"<title>{tip}</title></rect>"
        )

    svg = f'<svg viewBox="0 0 {W:.0f} {H:.0f}" preserveAspectRatio="none" role="img" ' \
          f'aria-label="average response time per hour, last 24 hours">'
    if path:
        svg += f'<path d="{path}"/>'
        li, lv = points[-1]
        svg += f'<circle cx="{x(li):.1f}" cy="{y(lv):.1f}" r="2.5"/>'
    svg += rects + "</svg>"

    peak = max((v for _, v in points), default=0.0)
    return (
        f'<div class="latency-graph" title="response time last 24h (avg per hour), peak {int(round(peak))} ms">'
        f"{svg}</div>"
    )


def render_host(entries: list[dict], yearly: list[dict], host: str, now: datetime) -> str:
    host_runs = [e["hosts"][host] for e in entries if host in e.get("hosts", {})]
    current = host_runs[-1] if host_runs else None
    current_status = current["status"] if current else "nodata"

    uptimes = []
    for label, days in WINDOWS:
        pct, _ = uptime(entries, host, now - timedelta(days=days))
        uptimes.append(
            f'<div class="uptime"><span class="uptime-label">{label}</span>'
            f"<span>{'—' if pct is None else f'{pct:.2f}%'}</span></div>"
        )
    alltime = None
    if yearly:
        ok = total = 0
        for day in yearly:
            bucket = day.get("hosts", {}).get(host)
            if bucket:
                ok += bucket.get("ok", 0) + bucket.get("warning", 0)
                total += day.get("total", 0)
        alltime = round(100 * ok / total, 2) if total else None
    uptimes.append(
        f'<div class="uptime"><span class="uptime-label">all-time</span>'
        f"<span>{'—' if alltime is None else f'{alltime:.2f}%'}</span></div>"
    )

    latency = f' · {current["latency_ms"]} ms' if current and current.get("latency_ms") is not None else ""
    error_line = (
        f'<p class="error-line">⚠ {html.escape(current["error"])}</p>'
        if current and current.get("error")
        else ""
    )

    return (
        f'<section class="host" id="host-{html.escape(host)}">'
        f'<div class="host-head"><h2>{html.escape(host)}</h2>{badge(current_status)}'
        f'<span class="latency">{latency.strip(" ·")}</span></div>'
        f'{error_line}'
        f'{day_strip(entries, host, now)}'
        f'{latency_graph(entries, host, now)}'
        f'{category_grid(current.get("categories") if current else None)}'
        f'<div class="uptimes">{"".join(uptimes)}</div>'
        f"</section>"
    )


def render_incidents(entries: list[dict]) -> str:
    incidents = []
    for entry in reversed(entries):
        for name, result in entry.get("hosts", {}).items():
            if result.get("status") == "error" or result.get("status") == "warning":
                detail = result.get("error")
                if not detail:
                    cats = normalize_categories(result.get("categories") or {})
                    parts = [
                        f"{cat}: error" for cat in ("infrastructure", "workloads")
                        if (cats.get(cat) or {}).get("error")
                    ]
                    parts += [
                        f"{cat}: warning" for cat in ("infrastructure", "workloads")
                        if not (cats.get(cat) or {}).get("error") and (cats.get(cat) or {}).get("warning")
                    ]
                    detail = ", ".join(parts) or "degraded"
                incidents.append(
                    f'<li><span class="ts">{entry["timestamp"]}</span> '
                    f"<strong>{html.escape(name)}</strong> "
                    f'{badge(result["status"])} — {html.escape(str(detail))}</li>'
                )
        if len(incidents) >= MAX_INCIDENTS:
            break
    if not incidents:
        return '<p class="muted">No incidents recorded. 🎉</p>'
    return f'<ul class="incidents">{"".join(incidents[:MAX_INCIDENTS])}</ul>'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--out", default="site")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    now = datetime.now(timezone.utc)
    entries = load_daily(data_dir)
    yearly = load_yearly(data_dir)

    # Config is the source of truth for which hosts are displayed; entries of
    # removed hosts stay in the history files but vanish from the page.
    config = load_json(Path("config/hosts.json"), None)
    hosts: list[str] = []
    if config and config.get("hosts"):
        hosts = [h["name"] for h in config["hosts"]]
    else:
        for entry in entries:
            for name in entry.get("hosts", {}):
                if name not in hosts:
                    hosts.append(name)

    if entries:
        current = dict(entries[-1]["hosts"])
        overall = worst([r.get("status", "error") for r in current.values()])
        overall_message = {
            "ok": "All systems operational",
            "warning": "Some systems degraded",
            "error": "Outage detected",
        }[overall]
    else:
        current, overall = {}, "nodata"
        overall_message = "No data yet — first check pending"

    content = "".join(render_host(entries, yearly, host, now) for host in hosts)
    incidents = render_incidents(entries)
    generated = now.strftime("%Y-%m-%d %H:%M:%S UTC")

    template = TEMPLATE.read_text(encoding="utf-8")
    page = (
        template.replace("<!-- OVERALL_CLASS -->", overall)
        .replace("<!-- OVERALL_MESSAGE -->", html.escape(overall_message))
        .replace("<!-- CONTENT -->", content)
        .replace("<!-- INCIDENTS -->", incidents)
        .replace("<!-- GENERATED -->", html.escape(generated))
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Machine-readable status resource (served as /status.json).
    api_hosts = {}
    for host in hosts:
        host_runs = [e["hosts"][host] for e in entries if host in e.get("hosts", {})]
        current = host_runs[-1] if host_runs else None
        up = {}
        for label, days in WINDOWS:
            pct, _ = uptime(entries, host, now - timedelta(days=days))
            up[label] = pct
        alltime = None
        if yearly:
            ok = total = 0
            for day in yearly:
                bucket = day.get("hosts", {}).get(host)
                if bucket:
                    ok += bucket.get("ok", 0) + bucket.get("warning", 0)
                    total += day.get("total", 0)
            alltime = round(100 * ok / total, 2) if total else None
        up["all-time"] = alltime
        api_hosts[host] = {
            "ok": bool(current and current.get("status") != "error"),
            "status": current["status"] if current else "nodata",
            "latency_ms": current.get("latency_ms") if current else None,
            "http_status": current.get("http_status") if current else None,
            "categories": current.get("categories") if current else None,
            "error": current.get("error") if current else None,
            "uptime": up,
        }

    # Static assets (favicons etc.) ship with the template.
    for asset in TEMPLATE.parent.iterdir():
        if asset.is_file() and asset.name != TEMPLATE.name:
            shutil.copy(asset, out_dir / asset.name)

    (out_dir / "index.html").write_text(page, encoding="utf-8")

    status = {
        "generated": generated,
        "overall": overall,
        "overall_message": overall_message,
        "hosts": api_hosts,
    }
    (out_dir / "status.json").write_text(
        json.dumps(status, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"generated {out_dir}/index.html ({len(entries)} runs, {len(hosts)} hosts, overall: {overall})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
