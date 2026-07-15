# OpenClaw handoff — repair the news/data cron pipeline (2026-07-15)

## (a) What we still depend on OpenClaw for

Per `CLAUDE.md` §13 and this dev machine's own trust boundary
(`NEXT_AGENT_PLAN.md` "⛔ Trust boundary"), **all scheduling lives on the
OpenClaw machine**, never here:

- Hourly-ish crawling: Reddit/GitHub/GDELT raw collection
  (`scripts/nightly_scan_ingest.py`) and news RSS ingestion
  (`scripts/ingest_news.py`) into `news_articles_raw` / `news_items`.
- The LLM chain (`stockmoney-scanner` skill: classify → catalyst synthesis →
  digest → weekend calibration narrator), run on OpenClaw's Sonnet-tier
  subscription — this dev machine has no `ANTHROPIC_API_KEY` wired and must
  not call `classify_scan.py` / `synthesize_catalysts.py` itself.
- Daily options-chain capture (`scripts/capture_options_snapshot.py`) — the
  one genuinely irreplaceable job; a missed day of IV surface / put-call
  ratio is lost forever.
- The cross-machine handshake: `scripts/export_for_sync.py` writes
  watermarked Parquet to `data_sync/exports/<table>/*.parquet` and commits +
  pushes it, since both machines' `.duckdb` files are gitignored by design.
  This dev machine only ever *reads* those committed Parquet files via
  `scripts/import_from_sync.py` — it never re-derives the data itself.

**Diagnosis just completed** (see `docs/cadence-news-report-2026-07-15.md`):
every one of the above stopped producing output around **2026-07-13 01:21-
08:14 ET** and nothing has landed since (now: 2026-07-15). Only one
`data_sync/exports` commit has EVER existed, from 2026-07-12 23:41-23:47 UTC
— the hourly `stockmoney-news-ingest-hourly` (`0 14-20 * * 1-5` UTC) and
daily `stockmoney-export-for-sync` (`20 21 * * *` UTC) cron jobs registered
per `OPENCLAW_CRON_REGISTERED.md` do not appear to have fired since. Also
worth noting as a design gap independent of the outage: `export-for-sync`
only runs **once a day** (21:20 UTC) even though news ingestion is hourly —
so even a fully healthy pipeline caps this dev machine's news freshness at
"up to ~24h behind," not "within the hour." The prompt below asks for both
the outage repair and a tightened export cadence.

## (b) Paste-ready prompt for the OpenClaw machine's agent

Paste everything in the fenced block below, verbatim, into the OpenClaw
machine's agent.

```
You are operating on the OpenClaw-capable machine for the "stockmoney"
project, repo checked out at /Users/danielisgod/Projects/stockmoney (per
OPENCLAW_CRON_REGISTERED.md — if your checkout path differs, use your actual
path but keep everything else the same). Read CLAUDE.md section 13 first for
the house rules on this integration (official/self-written skills only, no
ClawHub community skills, Haiku for high-volume/low-thinking passes, Sonnet
for deep reasoning, hourly cron for crawlers).

GOAL: the dev machine (a separate, non-OpenClaw machine that reads this
repo's git history and imports data via scripts/import_from_sync.py) has had
ZERO fresh news / options / macro data land since 2026-07-13 ~08:14 ET.
OPENCLAW_CRON_REGISTERED.md says 9 jobs are registered and enabled, but no
data_sync/exports/ commit has been pushed since 2026-07-12 23:47 UTC — the
very first one ever made. Find out why the registered jobs stopped
delivering, and fix it. Concretely:

1. Run `openclaw cron list` and paste the raw output. For every job listed
   in OPENCLAW_CRON_REGISTERED.md (stockmoney-options-snapshot,
   nightly-data-refresh, stockmoney-scan-ingest, stockmoney-news-ingest-hourly,
   stockmoney-export-for-sync, stockmoney-scan-classify,
   stockmoney-scan-catalyst-synthesis, stockmoney-scan-digest,
   stockmoney-calibration-narrator), check `openclaw cron get <id>` and
   report: enabled true/false, last run time, last run status, and — most
   important — whether "last run" is actually recent (within the last
   schedule interval) or stale/idle. If the OpenClaw daemon itself isn't
   running, or the machine was asleep/off, say so plainly.

2. If any job is disabled, missing, or has an error status, re-register or
   fix it so it matches OPENCLAW_CRON_REGISTERED.md's schedules exactly:
     - stockmoney-news-ingest-hourly: `0 14-20 * * 1-5` (UTC) →
       `uv run python scripts/ingest_news.py`
     - stockmoney-scan-ingest: `30 4 * * *` →
       `uv run python scripts/nightly_scan_ingest.py`
     - nightly-data-refresh: `0 21 * * *` →
       `uv run python scripts/nightly_refresh.py`
     - stockmoney-options-snapshot: `45 20 * * *` →
       `uv run python scripts/capture_options_snapshot.py`
     - stockmoney-export-for-sync: keep the existing job but CHANGE its
       schedule from once-daily (`20 21 * * *`) to hourly, immediately after
       each news-ingest run, e.g. `35 14-20 * * 1-5` (15 min after
       stockmoney-news-ingest-hourly) PLUS keep the existing `20 21 * * *`
       daily run so options/macro/OHLCV data (which land on their own
       once-daily schedules) still get exported too. Command stays:
       `uv run python scripts/export_for_sync.py && git add data_sync/ &&
       git commit -m "sync: export batch" && git push origin
       redesign/morning-briefing-arena` (skip the commit/push if
       export_for_sync.py reports 0 new rows — it already no-ops safely,
       see the script's own idempotency guarantees).
     - The four `stockmoney-scanner`-skill LLM passes (scan-classify,
       catalyst-synthesis, scan-digest, calibration-narrator) — re-register
       exactly as documented in OPENCLAW_CRON_REGISTERED.md if they're not
       running; do not modify skills/stockmoney-scanner/SKILL.md itself.

3. Do NOT install any ClawHub community skill for this — only
   skills/stockmoney-scanner (already in this repo) and the plain
   `command`-payload jobs above are permitted (CLAUDE.md §13, hard rule).
   Do not add a new ANTHROPIC_API_KEY anywhere; keep using the existing
   claude-cli subscription-tier routing already configured for the LLM jobs.

4. After fixing/re-registering, PROVE it end-to-end with real output, not a
   description:
     a. Run `uv run python scripts/ingest_news.py` once manually and paste
        its JSON output (expect keys like rss_raw / news_items_upserted).
     b. Run `uv run python scripts/export_for_sync.py` once manually and
        paste its JSON output (expect news_items/news_articles_raw with
        rows > 0 given step (a) just ran).
     c. Commit and push data_sync/exports/** (with the
        Co-Authored-By trailer this repo's convention uses) to
        `redesign/morning-briefing-arena` (or whatever branch
        OPENCLAW_CRON_REGISTERED.md's "checked out on" line says — update
        that file if the branch has changed).
     d. Update OPENCLAW_CRON_REGISTERED.md and OPENCLAW_SETUP_REPORT.md with
        what you found and fixed (root cause of the ~2.5 day outage — daemon
        down? machine off? a job silently erroring? — and the new
        export-for-sync cadence), commit that too.

SUCCESS CRITERIA (verify before declaring done):
  - `openclaw cron list` shows stockmoney-news-ingest-hourly and
    stockmoney-export-for-sync both `enabled: true` with a "last run" inside
    their own schedule interval (not stale).
  - A NEW commit touching `data_sync/exports/news_items/` and
    `data_sync/exports/news_articles_raw/` exists, pushed to the shared
    branch, dated after this session.
  - The dev machine, after `git pull` + running
    `.venv/bin/python scripts/import_from_sync.py`, should see
    `news_items` rows with `created_at` inside the last 2 hours (i.e. the
    News page's "近24h" count > 0). If you have shell access to verify this
    yourself against your own local DuckDB before pushing, check:
    `SELECT count(*) FROM news_items WHERE created_at >= now() - INTERVAL 2 HOUR`
    should be > 0.
  - Do not report success from job configuration alone ("I registered the
    cron") — only from the manual run's real JSON output plus the
    subsequent successful git push, per this repo's "evidence not
    self-report" convention (see OPENCLAW_SETUP_REPORT.md for the expected
    format/tone).
```

## Notes for whoever reviews this on the dev machine

- Even after the OpenClaw side is fixed, **someone still has to `git pull`
  and run `.venv/bin/python scripts/import_from_sync.py` on this dev
  machine** — that step is manual today (per `NEXT_AGENT_PLAN.md`'s trust
  boundary, this dev machine was never meant to run its own cron). Not fixed
  by this worker (out of file-ownership scope: `src/stockmoney/**` is
  foreman/backend territory, and standing up a local scheduler wasn't part
  of this task). Flagging so the foreman can decide whether a lightweight
  local puller (launchd/cron running `git pull && import_from_sync.py`
  hourly) is worth adding.
- `frontend/src/lib/refreshCadence.ts` was updated in this same session so
  that once fresh data does land, an already-open tab picks it up within an
  hour even outside market hours, instead of only on next manual focus/
  refresh. See `docs/cadence-news-report-2026-07-15.md`.
