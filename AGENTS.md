# AGENTS.md

Status page for consolving services (GitHub Pages). Python stdlib only —
**no dependencies, no build step**.

## Layout

- `config/hosts.json` — monitored hosts (`name` + full `url` to the host's
  health-status daemon, default port `11012`, not routed through traefik).
  Exception: `holonode` is VM 200 on host1 (Podman, no host1 Docker visibility);
  its daemon is reached through a DNAT on host1 (`:11013` → `10.20.30.142:11012`,
  see host1 README). The host-side checker uses the direct VM IP instead
  (`/opt/status-checker/hosts-local.json`), same host `name` so history merges.
- `scripts/check.py` — check all hosts, append to `data/` history files,
  prune retention. Run every 15 min (host-side timer, backup workflow).
- `scripts/generate.py` — render `site/index.html` from `data/`.
- `assets/template.html` — template with `<!-- CONTENT -->` and `<!-- META -->`
  placeholders.
- `assets/favicon.svg|favicon-32.png|apple-touch-icon.png` — copied verbatim
  into `site/` by the generator (everything except `template.html`).
- `data/` — history, committed by CI. Never hand-edit except for fixes.

## History files

- `data/daily/YYYY-MM-DD.json`, `data/weekly/YYYY-Www.json`,
  `data/monthly/YYYY-MM.json`: raw entries, one per run
  (`{"timestamp": ..., "hosts": {...}}`), compact JSON, append-only.
- `data/yearly/YYYY.json`: one aggregated entry per day, rebuilt from the
  daily file on every run (no drift).
- Retention constants live at the top of `check.py`.

## Status semantics

- Host status = worst severity across `infrastructure` / `workloads` counts
  of the daemon response — there is no `unknown` bucket; unmatched services
  count as `workloads`.
- `error` = unreachable / non-200 / invalid JSON or any `error > 0`,
  `warning` = any `warning > 0`, else `ok`.
- Uptime = runs without `error` / total runs.
- Legacy history entries (categories `private` / `public` / `customers` /
  `unknown`) are normalized on read: `private` → `infrastructure`,
  everything else → `workloads`.

## Workflow

- Primary check paths: **host-side** — `status-checker.timer` on host1
  (:12/:27/:42/:57) and host2 (:05/:20/:35/:50, staggered) each run
  `scripts/check.py` and push `data/` (per-host deploy keys). Pushes trigger
  `deploy-on-push.yml`; the stagger avoids same-instant push races.
- Backup: `.github/workflows/status.yml` (`*/5` cron + dedupe step,
  `status-bot` commits) — check + commit only, **no deploy steps** (GitHub
  drops ~90% of its scheduled events, so deploys never hang off it).
- Secrets: `HEALTH_STATUS_TOKEN` (bearer token for the daemons).
- Do not split deploy-on-push into the scheduled workflow.

## Dedupe semantics

- Both check paths append to `data/`; entries carry their own UTC timestamps,
  so gaps/duplicates never distort uptime math (uptime = runs without error /
  total runs).
- The scheduled workflow's dedupe step skips runs when the newest `data/`
  commit is younger than 13 minutes.

## Conventions

- Scripts stay runnable with plain `python3` on ubuntu-latest and macOS.
- Timestamps: UTC, ISO 8601. Week numbers: ISO.
- Keep the HTML self-contained (inline CSS, no external assets) so the site
  works offline and needs no CDN.

## Related

- Health daemon (per host): `../health-status` — private repo on git-lab.de.
- DNS `status.consolving.de` → `consolving.github.io`, repo is public
  (Pages requires it), site: https://status.consolving.de
