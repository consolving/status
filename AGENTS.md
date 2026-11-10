# AGENTS.md

Status page for consolving services (GitHub Pages). Python stdlib only —
**no dependencies, no build step**.

## Layout

- `config/hosts.json` — monitored hosts (`name` + full `url` to the host's
  health-status daemon, default port `11012`, not routed through traefik).
- `scripts/check.py` — check all hosts, append to `data/` history files,
  prune retention. Run every 30 min by the workflow.
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

- `.github/workflows/status.yml`: cron `7,37 * * * *` (every 30 min, offset
  from :00/:30 — GitHub drops queued scheduled jobs at the top of the hour),
  concurrency group `status-check`, commits `data/` as `status-bot`, deploys
  `site/` via actions/deploy-pages. Do not split into multiple workflows.

## Conventions

- Scripts stay runnable with plain `python3` on ubuntu-latest and macOS.
- Timestamps: UTC, ISO 8601. Week numbers: ISO.
- Keep the HTML self-contained (inline CSS, no external assets) so the site
  works offline and needs no CDN.

## Related

- Health daemon (per host): `../health-status` — private repo on git-lab.de.
- DNS `status.consolving.de` → `consolving.github.io`, repo is public
  (Pages requires it), site: https://status.consolving.de
