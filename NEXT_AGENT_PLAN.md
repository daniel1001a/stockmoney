# Next Agent Plan — for Agent 4 (dev machine, `/Users/danielkang/Documents/stockmoney-main`)

> Paste this whole file (or the short prompt the owner gives you) as your
> opening context. You continue the stockmoney build on the **dev machine**.
> A separate agent (Agent 3) runs on the OpenClaw machine and owns everything
> scheduled/LLM — see the trust boundary below. Read this fully before touching
> code.

## Branch
Work on `redesign/morning-briefing-arena` (pushed, **not** merged to `main` —
do not merge without owner review). `git pull` first; commit + push each Wave.
End commit messages with: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

## Read first
- `CLAUDE.md` — hard boundaries + discipline (§2 look-ahead, §12 feature
  significance test, §13 no thinking-models locally).
- `IMPROVEMENT_PLAN.md` — alpha roadmap S1–S7 (S2/S3 are your Waves C/D).
- `OPENCLAW_AGENT_PLAN.md` — what Agent 3 is doing. **You depend on its outputs.**
- Memory: `~/.claude/projects/-Users-danielkang-Documents-stockmoney-main/memory/`
  (`frontend-redesign-2026-07`, `stockmoney-local-env`,
  `alpha-options-microstructure-2026-07`) — real-vs-seed data, env, data ceiling.

## Local environment
- Use `.venv/bin/python` directly (no `uv` on PATH). Before data scripts:
  `set -a; source .env; set +a`.
- API: `.venv/bin/python -m uvicorn stockmoney.api.main:app --port 8000`
- Frontend: `npm --prefix frontend run dev` (Vite :5173, proxies /api → :8000)
- DBs (both gitignored): demo `data/stockmoney.duckdb` (rebuild:
  `scripts/seed_demo_data.py`), live `data/stockmoney_live.duckdb` (rebuild:
  `scripts/build_live.py`).
- Keys: FRED ✅, GDELT/GCP creds ✅ (BigQuery auth tested working on this
  machine too), `ANTHROPIC_API_KEY` intentionally **empty** (see boundary).
  `secrets/` and `.env` are gitignored — never commit them.

## ⛔ Trust boundary — what is OUTSOURCED to Agent 3 (OpenClaw). Do NOT build these:
- **All scheduling / cron** (daily options-snapshot capture, nightly refresh,
  news ingestion). You write scripts to be *schedulable* (pure, idempotent,
  `--db` param); Agent 3 schedules them.
- **The LLM chain**: Reddit/GDELT scrape → Haiku classify → Sonnet catalyst
  synthesis → `catalyst_signals` + the 催化劑推理 `news_items` type. Runs on
  Agent 3's subscription tier. Do **not** wire `ANTHROPIC_API_KEY` here, do
  **not** call `scripts/classify_scan.py` / `synthesize_catalysts.py` locally.
- **Notifications** (Slack/Discord digests).

### You TRUST Agent 3 via these committed artifacts (don't regenerate them):
- `OPENCLAW_CRON_REGISTERED.md` → tells you what's actually scheduled. **If it's
  absent, assume nothing is scheduled** and don't rely on fresh prod data.
- `data_sync/exports/**.parquet` → the real captured rows (irreplaceable
  per-symbol options history; `catalyst_signals`; `news_items`). Import these.
- `OPENCLAW_SETUP_REPORT.md` → done-report + blockers.

### Your side of the handshake (this is a Wave-0 task — do it early):
Write `scripts/import_from_sync.py` — the importer that consumes
`data_sync/exports/<table>/*.parquet` (Agent 3's exporter output) into the local
live DB, idempotently (dedup on each table's PK / latest `ingested_at`; safe to
re-run; track a local watermark so re-imports don't duplicate). Test it end to
end the first time Agent 3 pushes a real Parquet file. Until then, stub + unit-
test against a hand-made Parquet. **This is the only way per-symbol options
history and the LLM catalyst outputs reach your machine** — `.duckdb` files are
gitignored on both sides by design.

---

## Plan — do A → B → C → D, one commit-group per Wave, tests + real verification each.

### Wave 0 — Sync importer (unblocks trusting Agent 3)
`scripts/import_from_sync.py` per the handshake above + a unit test on a sample
Parquet. Ship even before Agent 3's first export exists.

### Wave A — Go-live foundations (local, no key, low risk)
- **A1. `STOCKMONEY_DB` env override.** API hardcodes `data/stockmoney.duckdb`;
  add an env-var override in `src/stockmoney/data/db.py` + `api/db.py` so the app
  can point at the live DB without code edits. Add a test.
- **A2. Fix regime labels.** Real GMM cluster ids are arbitrary, so the current
  "趨勢多頭/空頭" labels are meaningless on live data **and semantically wrong** —
  regimes measure volatility/trend *strength*, not up/down direction. Label each
  cluster from its centroid (realized_vol / adx / dispersion) as e.g. "高波動趨勢
  / 低波動盤整 / 中性", and fix the same up/down misconception in the frontend +
  `api/queries.py` `REGIME_LABELS`. Honesty fix, not cosmetic.

### Wave B — Turn on unlocked free data (GDELT)
- Wire GDELT into the feature pipeline and run the **same paired-bootstrap
  significance test** the VIX candidate got
  (`models/backtest_vix_term_ablation.py` is the template; `gdelt_sentiment`
  feature already exists). Promote to `FEATURE_COLUMNS` **only if 95% CI < 0**;
  otherwise keep as candidate and report honestly.
- Keep BigQuery usage inside the free 1 TB/mo (the events query scans ~16 GB/day;
  do **not** backfill years — bound the window). Scheduling of this is Agent 3's.

### Wave C — Alpha, the real money (highest value + highest rigor)
- **S2 VRP target** (IMPROVEMENT_PLAN.md §S2): predict *forward realized vol vs
  entry implied vol* (VRP), not raw direction — direction can be right while a
  long option dies to theta/IV-crush. **Market-level VRP (VIX vs realized) is
  computable now — do that first.** Per-symbol VRP needs accumulated daily
  option-snapshot history, which only exists once Agent 3's capture cron has run
  for weeks — gate that sub-part on the `data_sync` imports, don't block on it.
- Look-ahead HIGH RISK: write a short plan first, add leakage-canary tests, full
  ablation, honest report.

### Wave D — Make the Arena real (S3)
- Real simulated-trading engine (IMPROVEMENT_PLAN.md §S3): each trader "places"
  simulated option orders from its own prediction, scored on **real option P&L**
  (`option_selection.py` + `options_pnl.py` already exist). Turns the leaderboard
  from seed into genuinely live. Needs the same option-snapshot history as C's
  per-symbol part.

---

## Pre-launch checklist (surface to the owner as you go)
- LLM chain runs on OpenClaw (decided). You do not need an ANTHROPIC key here.
- **Before any non-local exposure:** harden API security — `api/main.py` CORS is
  dev-only (localhost). No auth yet. Flag before deploying beyond localhost.
- **Most urgent, owner-facing:** until Agent 3 schedules the daily options
  snapshot, per-symbol options history is being lost every trading day. This
  gates Waves C(per-symbol)/D. Keep it loud in every status update.

## Rules that don't change
- Keep the demo DB intact and demoable; do real/heavy work in the live DB.
- Look-ahead discipline (`available_at`); new features must pass the significance
  test before promotion; report "tried, didn't help" honestly as a real result.
- Each Wave: run tests + actually exercise it (frontend → open the browser,
  backend → pytest), green before moving on. Don't fix one thing and break another.

Start with Wave 0, then A. Only ask the owner when a Wave has a genuine fork
only they can decide; otherwise follow the plan and report evidence per Wave.
