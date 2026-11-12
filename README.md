# consolving status

Static status page for consolving services, deployed via GitHub Pages at
**https://status.consolving.de**.

A GitHub Action re-renders the static HTML page from the check history on
every push. Checks run every 15 minutes. Two check paths exist:

1. **Primary — host-side checkers**: systemd timers on `host1.consolving.net`
   (:12/:42) and `host2.consolving.net` (:27/:57) each run `scripts/check.py`
   every 30 min, interleaved so `data/` gets a push every 15 minutes, and
   push to GitHub via deploy keys (`/opt/status-checker/` on both hosts,
   deploy keys `status-checker@host1`/`@host2` in the repo).
   pushes `data/` to GitHub via a deploy key (`/opt/status-checker/`).
   Every push to `main` triggers `.github/workflows/deploy-on-push.yml`,
   which re-renders and deploys the page. This path is reliable — GitHub
   drops the vast majority of scheduled workflow events.
2. **Backup — scheduled workflow** (`status.yml`, `*/15` + dedupe to ~15 min):
   checks + commits only, no deploy. Exists in case host1 is down.

## How it works

```
GitHub Action (cron */30)
  └─ scripts/check.py       checks every host from config/hosts.json
  │                           └─ appends entry to data/daily|weekly|monthly/
  │                           └─ updates per-day summary in data/yearly/
  │                           └─ prunes history beyond retention
  ├─ commits data/ back to the repo (history lives in git)
  └─ scripts/generate.py    renders site/index.html from the history files
                              └─ deployed to GitHub Pages
```

Each checked host runs the small [health-status](../health-status) daemon
(private repo on git-lab.de) which answers with a JSON like:

```json
{
  "status": "ok",
  "timestamp": "2026-11-09T12:00:00Z",
  "infrastructure": {"ok": 3,  "warning": 0, "error": 0},
  "workloads":      {"ok": 24, "warning": 0, "error": 0}
}
```

## Status semantics

- A host is **error** when unreachable, answering non-200/invalid JSON, or
  when any of `infrastructure` / `workloads` reports `error > 0`.
- **Warning** when any of them reports `warning > 0`, else **ok**.
- Uptime = runs without `error` / total runs.

## Repository layout

| Path | Purpose |
|---|---|
| `.github/workflows/status.yml` | Cron check + history commit + Pages deploy |
| `config/hosts.json` | Hosts to monitor (name + full health URL) |
| `scripts/check.py` | Runs the checks, writes history files |
| `scripts/generate.py` | Renders `site/` from history |
| `assets/template.html` | HTML template used by the generator |
| `data/` | History files, committed by the action |
| `CNAME` | Custom domain for GitHub Pages (`status.consolving.de`) |

## Adding a host

Edit `config/hosts.json`:

```json
{
  "hosts": [
    { "name": "host1.consolving.net", "url": "http://host1.consolving.net:11012/health" }
  ]
}
```

The URL points to the health-status daemon of that host — **not** routed
through traefik, so the check still works when traefik is down. The daemon
listens on its own port (default `11012`).

## History & retention

Raw check results (one entry per run) are stored per period:

| Period | File | Retention |
|---|---|---|
| Daily | `data/daily/YYYY-MM-DD.json` | 90 days |
| Weekly | `data/weekly/YYYY-Www.json` | 2 years |
| Monthly | `data/monthly/YYYY-MM.json` | 2 years |
| Yearly | `data/yearly/YYYY.json` | forever, aggregated per day |

Retention is applied on every run by `scripts/check.py` (constants at the top
of the file).

## Running locally

```bash
python3 scripts/check.py                 # run checks, update data/
python3 scripts/generate.py              # rebuild site/index.html
python3 scripts/check.py --config config/hosts.json --data-dir data
```

Both scripts are Python stdlib only — no dependencies.

## Setup (one-time)

1. Push this repo to GitHub. The repo must be **public** — GitHub Pages for
   private repos needs a paid plan.
2. Pages: enabled once via API (`gh api -X POST repos/consolving/status/pages
   -f build_type=workflow`); the org's GITHUB_TOKEN policy does not allow the
   workflow to enable it itself.
3. DNS: `status.consolving.de` → `CNAME consolving.github.io.`
   (the `CNAME` file in this repo must contain `status.consolving.de`).

## Notes

- Hosts must be reachable from GitHub-hosted runners — public IPs / open
  health ports only, no LAN-only services.
- Scheduled workflows are delayed during GitHub load peaks and get disabled
  after 60 days of repo inactivity (any push resets the timer; data commits
  count as activity).
