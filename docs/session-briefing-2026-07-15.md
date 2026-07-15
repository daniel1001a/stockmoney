# Session Briefing — 2026-07-15 (data-integrity + features)

For your review. Branch `research/edge-hunt-2026-07` (not merged to main). 5 commits, all tests green before each commit.

---

## 1. The bug that made you angry — impossible strikes (B2)

**What you saw:** `NFLX 996.7 Call`, `TSLA 234.6`, `SOXX 247.3`, `MU 185.1`, `WFC 74.3` — strikes that don't exist on any real option chain.

**Root cause (two independent sources, both found by reading code + querying the live DB, not guessed):**
1. **The one you actually saw** — `scripts/seed_demo_data.py` generated strikes as `round(entry_px × 1.03, 1)`. This script is the *only* current writer of `trader_trades` (the live engine doesn't populate them yet), so it produced every fractional strike on the board.
2. **The real engine path** — `option_selection.strike_for_delta()` returns a raw Black-Scholes-inverted strike; `option_bridge.py` passed it straight through unrounded.

**Fix:**
- New `models/strike_ladder.py` — `snap_strike()` maps any price to a real listed strike (ladder: <$25→$0.5, $25–100→$1, $100–250→$2.5, $250–500→$5, ≥$500→$10). Verified: 996.7→**1000**, 234.6→**235**, 247.3→**247.5**.
- Engine path: `option_bridge` now calls `select_option(..., snap=True)`; research backtests keep the smooth continuum (`snap=False` default).
- Seed script + `api/cockpit.py` sell-put suggestion now snap too — so nothing generates a fake strike again.
- **Existing rows repaired** in *both* DuckDB files (with backups): 62 strikes snapped, 58 non-Friday expiries realigned to Fridays. **0 off-ladder strikes, 0 non-Friday expiries remain.** Touched only strike/expiry — never premium/P&L (proven independent).

**Guardrail so it can't recur:** new `data/validation.py` (strike-ladder, Friday-expiry, premium-plausibility, no-future-timestamp, P&L-sign, win-rate reconciliation) + failing tests. Full audit in `docs/data-integrity-audit-2026-07-15.md`.

---

## 2. Live Board "5天前 / 12天前" confusion (B1)

**Not a sort bug** — the feed was already newest-first (`ORDER BY entry_at DESC`). The real issues were *labeling* and *stale data*: open positions opened days ago read like mistaken "today's trades."

**Fix (Arena.tsx):** timestamps now say **"N天前進場" / "N天前平倉"**, and each row shows an unambiguous coloured outcome state — **持倉中** (sky) / **獲利平倉** (green) / **虧損平倉** (red) — with a matching left-border accent for at-a-glance scanning. (This also delivers your P/L-clarity request, F2.)

---

## 3. Hollow homepage panel → actionable prep (F4)

Killed the three useless tiles (raw VIX, `期限結構` jargon, meaningless watchlist `今日漲跌家數`). Replaced with three tiles that each do real pre-trade work:
- **選擇權環境** — VIX reframed as "premium 偏貴(利於賣方) / 偏便宜(利於買方)".
- **今日焦點** — the biggest watchlist mover today (where to look first).
- **最近財報** — nearest earnings date (your main event risk).

All with honest `資料不足` fallbacks.

---

## 4. Homepage prediction accuracy (F3)

New **🎯 模型方向預測戰績** strip surfacing the direction model's historical hit-rate + rolling window (from `/api/predictions`), held to the same standard as the arena traders — framed as transparency, not a call to act. Shows honest `資料不足(尚無已結算預測)` until predictions settle.

---

## 5. Post-market wrap-up (F1)

New **📉 今日盤後總結** card + `/api/postmarket` endpoint + `models/postmarket_wrap.py` (12 tests). An honest end-of-day desk note: headline, narrative, top movers with drivers — and **"查無明確相關消息"** when a move has no matching news (never a fabricated cause). No look-ahead (all data bounded to as-of date).

---

## 6. News is stale + refresh cadence (C1/C2/C3)

**News staleness is NOT a code bug.** Evidence: every ingestion table stopped within one window ~2.5 days ago; this machine has no crontab and no auto-pull step. **Root cause: the hourly crawler cron lives on your OpenClaw machine and stopped firing ~2.5 days ago**, and there's no local `git pull + import_from_sync` step here.

- **Cadence fixed** (`refreshCadence.ts`): after-hours now polls **hourly** (was: not at all); market-hours stays fast. Tests updated.
- **↳ ACTION FOR YOU:** run the paste-ready prompt in **`docs/openclaw-handoff-2026-07-15.md`** on your OpenClaw machine — it tells that machine's agent to find which cron jobs died, repair them, tighten `export-for-sync` to run after each hourly ingest, and prove it with real output. Details also in `docs/cadence-news-report-2026-07-15.md`.

---

## Evidence
- **Browser-verified live**: real strikes + Friday expiries on the board, the 3 coloured states, the new homepage tiles, the track-record strip, the 盤後總結 card. No console errors; `/api/postmarket`, `/api/predictions`, `/api/cockpit`, `/api/briefing`, `/api/trader-trades` all 200/valid JSON.
- **Tests**: frontend 28/28 pass + `tsc` clean; backend 629 pass (1 pre-existing unrelated SOXL failure, confirmed present without our changes).

## Commits (research/edge-hunt-2026-07)
```
65ef859 Session docs: plan + log for 2026-07-15
40dd7b9 Homepage + Live Board UI: post-market card, model track-record, actionable prep panel, P/L clarity
d0ca530 Post-market wrap-up: end-of-day desk note module + /api/postmarket endpoint
7fd52bc Cadence: hourly after-hours polling + news-staleness diagnosis + OpenClaw handoff
9b1722f Data integrity: snap option strikes to real ladder + validation layer + audit
```

## How the work was run (model/effort discipline)
- **Opus (this session)** = brain: planning, cross-file integration decisions, the approved DB migration, review.
- **Sonnet · high** workers, parallel, non-overlapping file ownership: W1 strikes+audit, W2 cadence+OpenClaw, W3 post-market, then a verify+commit worker (browser verification + tests + commits). Zero file-conflict, no rework.

## Decisions needing you / open items
1. **Run the OpenClaw handoff** (above) — the only thing this machine can't self-heal.
2. DB backups are in the session scratchpad (`stockmoney_live.duckdb.bak-2026-07-15`, `stockmoney.duckdb.bak-2026-07-15`) — restorable if you dislike the strike migration.
3. Audit noted several CLAUDE.md §2 sources are **unpopulated** (macro, fund-flow, alt-data, watchlist candidates) — a coverage gap, not corruption, worth knowing.
4. Pre-existing `tests/test_scoreboard.py` failure (empty SOXL feature matrix) is unrelated — left as-is.
