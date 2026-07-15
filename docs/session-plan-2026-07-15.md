# Session Plan — 2026-07-15 (data-integrity + features)

Branch: `research/edge-hunt-2026-07`. Commit per workstream; never merge to main.
Overriding theme: **eliminate incorrect / absurd data**. Data-integrity bugs first.

## Verified root causes
- **B2 impossible strikes** (`NFLX 996.7 Call`): `option_selection.strike_for_delta()` returns a continuous Black-Scholes strike, never snapped to a real listed strike. Frontend prints it verbatim. Fix = snap to a realistic strike ladder + reprice; apply in `league/option_bridge.py` (arena) and decide backtest path.
- **B1 board "5天前/12天前"**: `recent_trader_trades` already sorts `entry_at DESC` (queries.py:681). Real issue = freshness + labeling: open positions opened days ago, held, read like "today's trades"; ties to stale pipeline (news 2d old). Verify timestamps real; make "opened X ago / still holding vs closed" unambiguous (F2).
- **F4 hollow panel**: Opportunities.tsx renders VIX + `期限結構` + `今日漲跌家數` (watchlist breadth) — the useless metrics flagged.
- **C2 cadence**: refreshCadence.ts = 1min power-hour / 3min midday / **null after-hours**. Want max in-hours + hourly after-hours.

## Dispatch (3 workers parallel + foreman)
File ownership partitioned → zero shared-file races.

- **W1 (Sonnet·high)** — strikes + data-integrity validation + audit hunt. Owns `models/`, `data/`, `league/option_bridge.py`, `tests/` (existing files). Deliver strike ladder+reprice, validation module+failing tests, `docs/data-integrity-audit-2026-07-15.md`.
- **W2 (Sonnet·high)** — cadence + news-staleness diagnosis + OpenClaw handoff. Owns `lib/refreshCadence.ts`, `scripts/` ingestion, `docs/openclaw-handoff-2026-07-15.md`.
- **W3 (Sonnet·high)** — post-market wrap-up backend. Owns ONLY new `models/postmarket_wrap.py` + its test.
- **Foreman (Opus)** — owns `api/queries.py`, `api/main.py`, `frontend/pages/*`, `lib/format.ts`. Wires W1/W3, Live Board P/L 3-state + freshness, panel redesign (F4), homepage prediction accuracy (F3), post-market UI (F1), relTime, browser verification, briefing.

Ordering: W1‖W2‖W3 now → foreman integrates on return → verify in browser → commit per workstream → `docs/session-briefing-2026-07-15.md`.
