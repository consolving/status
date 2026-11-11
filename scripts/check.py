#!/usr/bin/env python3
"""Check all configured hosts and append the result to the history files.

Runs every 15 minutes (host-side systemd timer on host1, backup:
.github/workflows/status.yml). Python stdlib only.

Usage:
    python3 scripts/check.py [--config config/hosts.json] [--data-dir data]
"""

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

# Retention: how many period files to keep (applied after every run).
RETENTION = {"daily": 90, "weekly": 104, "monthly": 24}  # yearly: keep all

CHECK_TIMEOUT = 30  # seconds per host (host1's daemon takes ~6s: many docker inspects + ldap + hairpin)
SEVERITY = {"ok": 0, "warning": 1, "error": 2}
STATUS_CATEGORIES = ("infrastructure", "workloads")

LEGACY_CATEGORIES = {
    "private": "infrastructure",
    "public": "workloads",
    "customers": "workloads",
    "unknown": "workloads",
}


def normalize_categories(payload: dict) -> dict:
    """Normalize a daemon payload to the current categories.

    Accepts legacy responses (private / public / customers / unknown) as well
    as the current ones (infrastructure / workloads). Everything legacy lands
    in infrastructure or workloads; there is no separate unknown bucket.
    """
    counts = {
        "infrastructure": dict(payload.get("infrastructure") or payload.get("private") or {}),
        "workloads": dict(payload.get("workloads") or {}),
    }
    for legacy, target in (("public", "workloads"), ("customers", "workloads"), ("unknown", "workloads")):
        for key, value in (payload.get(legacy) or {}).items():
            counts[target][key] = counts[target].get(key, 0) + int(value)
    return {
        cat: {
            "ok": int(counts[cat].get("ok", 0)),
            "warning": int(counts[cat].get("warning", 0)),
            "error": int(counts[cat].get("error", 0)),
        }
        for cat in STATUS_CATEGORIES
    }


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def compact(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as exc:
        print(f"warn: could not read {path}: {exc}", file=sys.stderr)
        return default


def append_to(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = load_json(path, [])
    if not isinstance(entries, list):
        print(f"warn: {path} has unexpected format, resetting", file=sys.stderr)
        entries = []
    entries.append(entry)
    path.write_text(compact(entries) + "\n", encoding="utf-8")


def check_host(host: dict, token: str = "") -> dict:
    """Check one host, return the result entry."""
    url = host["url"]
    result = {
        "ok": False,
        "status": "error",
        "latency_ms": None,
        "http_status": None,
        "categories": None,
        "error": None,
    }
    started = utcnow()
    try:
        headers = {"User-Agent": "consolving-status/1.0"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=CHECK_TIMEOUT) as resp:
            body = resp.read()
            result["http_status"] = resp.status
        result["latency_ms"] = int((utcnow() - started).total_seconds() * 1000)
        if result["http_status"] != 200:
            result["error"] = f"HTTP {result['http_status']}"
        else:
            payload = json.loads(body)
            counts = normalize_categories(payload)
            result["categories"] = counts
            worst = "ok"
            for cat in STATUS_CATEGORIES:
                c = counts[cat]
                if c["error"] > 0:
                    worst = "error"
                    break
                if c["warning"] > 0:
                    worst = "warning"
            result["status"] = worst
            result["ok"] = worst != "error"
    except urllib.error.HTTPError as exc:
        result["http_status"] = exc.code
        result["error"] = f"HTTP {exc.code}"
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        result["error"] = f"unreachable: {exc}"
    except json.JSONDecodeError as exc:
        result["error"] = f"invalid JSON: {exc}"
    return result


def prune(directory: Path, pattern: str, keep: int) -> None:
    if not directory.is_dir():
        return
    files = sorted(p for p in directory.iterdir() if re.fullmatch(pattern, p.name))
    for path in files[:-keep] if len(files) > keep else []:
        path.unlink()
        print(f"pruned {path}")


def update_yearly(data_dir: Path, today: str) -> None:
    """Rebuild today's per-day summary in the yearly file from the daily file."""
    daily = load_json(data_dir / "daily" / f"{today}.json", [])
    per_host: dict = {}
    for entry in daily:
        for name, result in entry.get("hosts", {}).items():
            bucket = per_host.setdefault(name, {"ok": 0, "warning": 0, "error": 0})
            bucket[result.get("status", "error")] += 1
    summary = {"date": today, "total": len(daily), "hosts": per_host}

    yearly_path = data_dir / "yearly" / f"{today[:4]}.json"
    yearly = load_json(yearly_path, [])
    yearly = [d for d in yearly if isinstance(d, dict) and d.get("date") != today]
    yearly.append(summary)
    yearly.sort(key=lambda d: d["date"])
    yearly_path.parent.mkdir(parents=True, exist_ok=True)
    yearly_path.write_text(compact(yearly) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/hosts.json")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--token", default=os.environ.get("HEALTH_STATUS_TOKEN", ""),
                        help="bearer token for the hosts' health-status daemons "
                             "(default: $HEALTH_STATUS_TOKEN)")
    args = parser.parse_args()

    config_path = Path(args.config)
    config = load_json(config_path, None)
    if not config or not config.get("hosts"):
        print(f"error: no hosts configured in {config_path}", file=sys.stderr)
        return 2

    now = utcnow()
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    day = now.strftime("%Y-%m-%d")
    week = now.strftime("%G-W%V")
    month = now.strftime("%Y-%m")
    year = now.strftime("%Y")
    data_dir = Path(args.data_dir)

    hosts = config["hosts"]
    with ThreadPoolExecutor(max_workers=min(8, len(hosts))) as pool:
        results = dict(zip([h["name"] for h in hosts],
                           pool.map(lambda host: check_host(host, args.token), hosts)))

    entry = {"timestamp": stamp, "hosts": results}
    for name, result in results.items():
        line = f"{stamp} {name}: {result['status']}"
        if result["latency_ms"] is not None:
            line += f" ({result['latency_ms']} ms, HTTP {result['http_status']})"
        if result["error"]:
            line += f" — {result['error']}"
        print(line)

    append_to(data_dir / "daily" / f"{day}.json", entry)
    append_to(data_dir / "weekly" / f"{week}.json", entry)
    append_to(data_dir / "monthly" / f"{month}.json", entry)
    update_yearly(data_dir, day)

    prune(data_dir / "daily", r"\d{4}-\d{2}-\d{2}\.json", RETENTION["daily"])
    prune(data_dir / "weekly", r"\d{4}-W\d{2}\.json", RETENTION["weekly"])
    prune(data_dir / "monthly", r"\d{4}-\d{2}\.json", RETENTION["monthly"])

    errors = sum(1 for r in results.values() if r["status"] == "error")
    print(f"checked {len(results)} hosts: {errors} error(s), data written to {data_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
