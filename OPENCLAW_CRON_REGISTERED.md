# OpenClaw Cron Registration — stockmoney

Written by Agent 3 (OpenClaw machine) as the handshake contract for Agent 4
(other machine, same repo, branch `redesign/morning-briefing-arena`). Agent 4
cannot inspect this machine's OpenClaw daemon directly — this file is the
evidence that scheduling exists. If it goes stale, assume nothing is
scheduled and ask Daniel to check `openclaw cron list` on this machine.

All times are UTC (cron host local timezone; no `--tz` override was set).
Working directory for every job: `/Users/danielisgod/Projects/stockmoney`,
checked out on `redesign/morning-briefing-arena`.

## Pure-Python data jobs (no LLM, `command` payload)

| Job | Cron expr | Days | Command | Purpose |
|---|---|---|---|---|
| `stockmoney-options-snapshot` | `45 20 * * *` | every day | `uv run python scripts/capture_options_snapshot.py` | **The non-negotiable one.** Dedicated capture of today's options chain (IV surface, put/call ratio, GEX/skew estimate), 15 min before `nightly-data-refresh` so an unrelated failure there can never cost this irreplaceable snapshot. Idempotent (dedups by latest `ingested_at`). |
| `nightly-data-refresh` | `0 21 * * *` | every day | `uv run python scripts/nightly_refresh.py` | Full pipeline: OHLCV, FRED macro, VIX term structure, options snapshot (redundant with above, harmless), news, features, predictions, league, attribution. **Pre-existing, confirmed still registered and healthy** (last run ok, 23h ago as of this writing). |
| `stockmoney-scan-ingest` | `30 4 * * *` | every day | `uv run python scripts/nightly_scan_ingest.py` | Raw RSS + Reddit content collection into `news_articles_raw`/`social_posts_raw`, post-Asia-close. **Pre-existing.** |
| `stockmoney-news-ingest-hourly` | `0 14-20 * * 1-5` | Mon–Fri | `uv run python scripts/ingest_news.py` | **New.** Hourly `news_items` refresh (10am–4pm ET) so the 消息雷達 stays fresh — addresses the owner's explicit feedback that news needs to update constantly, not once a day. |
| `stockmoney-export-for-sync` | `20 21 * * *` | every day | `uv run python scripts/export_for_sync.py` then `git add data_sync/ && git commit && git push origin redesign/morning-briefing-arena` (no-op commit skipped if nothing new) | **New.** Watermarked Parquet export of newly-written rows to `data_sync/exports/<table>/...`, committed and pushed so Agent 4 can import without ever touching this machine's local DuckDB file. Runs after both jobs above. |

## LLM-chain jobs (`stockmoney-scanner` skill, `agentTurn` payload)

All use `agentId: scanner`, model `claude-cli/claude-sonnet-4-6` (subscription
tier via claude-cli, **not** a paid API key — CLAUDE.md §13), tool allow-list
`exec,read` only. These were registered in an earlier session and are
**confirmed still active** below (re-verified via `openclaw cron get`, not
just assumed).

| Job | Cron expr | Days | Pass | Delivery |
|---|---|---|---|---|
| `stockmoney-scan-classify` | `30 0 * * 1-5` | Mon–Fri | Classification (Reddit/RSS → sentiment/candidate) | none |
| `stockmoney-scan-catalyst-synthesis` | `0 1 * * 1-5` | Mon–Fri | Catalyst transmission-chain synthesis | none |
| `stockmoney-scan-digest` | `0 7 * * 1-5` | Mon–Fri | Morning digest | WhatsApp `+16692619821` |
| `stockmoney-calibration-narrator` | `0 12,20 * * 0,6` | Sat/Sun | Weekend calibration campaign narrator | WhatsApp `+16692619821` |

`utilityModel`/Haiku-tier routing (CLAUDE.md §13's two-tier design) is not
separately wired into these cron jobs — all four passes currently run
Sonnet-tier end to end, matching what the `stockmoney-scanner` skill actually
implements today. Splitting the classification pass onto a Haiku-tier utility
model is a possible future optimization, not done in this session (noted as
a blocker/TODO in `OPENCLAW_SETUP_REPORT.md`, not silently skipped).

## Not registered (explicitly out of scope this session)

- TODO-OC-2 (crypto 24/7 scan), TODO-OC-3 (earnings/event reminders),
  TODO-OC-4 (method-update deep argumentation), TODO-OC-5 (unusual-options
  social flow) — `IMPROVEMENT_PLAN.md` §5 items not requested by
  `OPENCLAW_AGENT_PLAN.md` for this session.
- TODO-OC-1 (nightly league digest push) — needs a Slack/Discord webhook
  Daniel hasn't set up yet; see `OPENCLAW_SETUP_REPORT.md`.
