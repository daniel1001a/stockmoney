# News staleness diagnosis + refresh-cadence fix — 2026-07-15 (Worker W2)

## TL;DR

The News page's "近24h 0 則" is **not a bug in the freshness query or the
ingestion code**. It is an honest readout of a real gap: **no news ingestion
of any kind has run, on either machine, since 2026-07-13 08:14 ET** — about
47 hours before this report (now = 2026-07-15 07:01 ET). There is no cron on
this dev machine (confirmed: empty `crontab -l`, no launchd job), and the
OpenClaw machine's hourly news cron has not pushed a new sync export since
2026-07-12 23:47 UTC (the very first export it ever made). Root cause is an
**operational/scheduling gap**, not application code.

## Evidence

### 1. `news_freshness` query itself is correct
`src/stockmoney/api/queries.py:820-878` (`news_freshness`) does exactly what
the UI shows: `max(news_items.created_at)` for "last updated", a straight
`count(*) WHERE created_at >= now() - 24h` for the 24h count, and the most
recent `ingestion_runs` row where `target_table = 'news_articles_raw'` for
"上次抓取". All three read real tables with no filtering bug. Confirmed by
querying the live DB directly (read-only):

```
$ .venv/bin/python -c "duckdb.connect('data/stockmoney_live.duckdb', read_only=True)..."

max(news_items.created_at)      = 2026-07-13 08:14:55 ET
news_items count                = 122 (ALL of them dated 2026-07-13, single day)
news_items WHERE >= now()-24h   = 0
now()                            = 2026-07-15 07:01:36 ET
```

That is a genuine ~47h gap → matches the UI's "2 天前" / "近24h 0 則" exactly.

### 2. Last successful ingestion run, any table
```sql
SELECT target_table, max(finished_at), count(*) FROM ingestion_runs GROUP BY 1 ORDER BY 2 DESC;
```
```
news_articles_raw          2026-07-13 08:14:50 ET   (11 runs total)
options_derived_daily      2026-07-13 01:22:39 ET
put_call_ratio_daily       2026-07-13 01:22:39 ET
iv_surface_daily           2026-07-13 01:22:39 ET
vix_term_structure_daily   2026-07-13 01:21:50 ET
macro_series_daily         2026-07-13 01:21:49 ET
ohlcv_daily                2026-07-13 01:21:48 ET
market_index_ohlcv_daily   2026-07-12 20:48:04 ET
event_news_gdelt           2026-07-12 20:07:52 ET
social_posts_raw           2026-07-12 04:37:26 ET
```
**Every single table stopped updating around 2026-07-13 01:21-08:14 ET.**
This isn't a news-specific problem — it's the whole pipeline going quiet at
once, which points at "nothing has run" rather than "the RSS feed started
failing" (a feed-specific failure would still show later attempts logged
with `status='failed'`; there are none after 07-13 08:14 at all, success or
failure).

Last 15 `news_articles_raw` runs (all `source='rss'`, all `status='success'`,
`rows_written` 47-57 each time) show a roughly-hourly-to-several-hours-apart
cadence from 2026-07-09 through 2026-07-13 08:14, then **nothing** — not an
irregular gap consistent with occasional feed hiccups, but a hard stop.

### 3. Cross-machine sync is also stalled at the source
Per CLAUDE.md §13 and `OPENCLAW_CRON_REGISTERED.md`, the OpenClaw machine
(Agent 3) runs `stockmoney-news-ingest-hourly` (`0 14-20 * * 1-5` UTC =
10am-4pm ET Mon-Fri) and then `stockmoney-export-for-sync` which commits
watermarked Parquet to `data_sync/exports/**` and pushes. Checked this
machine's git history **and fetched `origin` to check for anything newer**:

```
$ git log --all --oneline -- data_sync/exports
1aa031e Register options/news/sync cron jobs and build cross-machine data export
```

Only **one** commit has ever touched `data_sync/exports/` — on any local
branch AND on `origin/redesign/morning-briefing-arena` after a fresh fetch.
That commit's exported Parquet run-ids (`ed6128f3`, `ea94af99`) exactly match
the two runs documented in `OPENCLAW_SETUP_REPORT.md`, both timestamped
2026-07-12 23:41-23:47 **UTC**. `data_sync/.watermark.json` (last written
2026-07-13 01:08 local, i.e. matches when this machine last imported/checked
sync state) confirms no table's watermark has advanced past 2026-07-12
19:41-19:52 ET. **No sync export has landed since the very first one**,
despite the hourly cron schedule that was supposedly registered on 2026-07-12.

### 4. This machine has no scheduler of its own
```
$ crontab -l        → "crontab: no crontab for danielkang"
$ ls ~/Library/LaunchAgents | grep stock   → (nothing)
```
Confirms `NEXT_AGENT_PLAN.md`'s stated trust boundary: **all scheduling is
outsourced to Agent 3 (OpenClaw)** — this dev machine was never meant to run
its own cron, it depends entirely on (a) Agent 3's hourly news cron actually
firing and pushing, and (b) *someone* on this machine periodically doing
`git pull` + `.venv/bin/python scripts/import_from_sync.py`. Neither has
happened in ~2 days.

## Root cause (summary)

**Two independent breaks, compounding:**
1. The OpenClaw machine's `stockmoney-news-ingest-hourly` /
   `stockmoney-export-for-sync` cron jobs, registered 2026-07-12 per
   `OPENCLAW_CRON_REGISTERED.md`, have not produced a second sync export in
   ~2.5 days despite an hourly Mon-Fri schedule that should have fired ~15+
   times since. Either the OpenClaw daemon stopped running the jobs, the jobs
   are failing silently, or the machine itself has been off/asleep.
2. Even if (1) is fixed, this dev machine has no mechanism to pull and import
   the new exports on its own — that step is manual (`git pull` +
   `import_from_sync.py`) and nobody has run it since 2026-07-13.

Neither is an application bug: `news_freshness` (src/stockmoney/api/queries.py),
`refresh_news_items` (src/stockmoney/data/news_synthesis.py, invoked by
`scripts/ingest_news.py`), and `import_from_sync.py`'s idempotent import logic
all look correct on inspection — there's simply nothing new for them to read
because nothing has been feeding them.

## What this worker did NOT do
Per file-ownership scope, did not touch `src/stockmoney/**`, did not run
`ingest_news.py` or `import_from_sync.py` against the live DB (that would be
a side-effecting write outside this worker's remit and outside the read-only
diagnostic mandate). See `docs/openclaw-handoff-2026-07-15.md` for the
paste-ready fix targeted at the OpenClaw machine.

## Frontend fix delivered alongside this diagnosis
`frontend/src/lib/refreshCadence.ts` — after-hours/overnight/weekend polling
changed from "stop entirely" (`null`) to hourly (`3_600_000` ms), specifically
so that once the OpenClaw-side fix lands, an already-open tab picks up fresh
news within an hour instead of only on next manual focus. See
`docs/openclaw-handoff-2026-07-15.md` and the PR/commit for details; tests in
`frontend/src/lib/refreshCadence.test.ts` updated and passing (5/5).
