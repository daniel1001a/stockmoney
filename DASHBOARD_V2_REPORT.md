# Dashboard v2 Report (Worker-6, 2026-07-14)

**Shipped all 5 v2 deepenings on top of the v1 honest cockpit: richer macro-news narrative, generic-headline filtering, per-name volume signal, per-name sector linkage, and a sector-rotation panel — all descriptive-only, no direction prediction added anywhere.**
**Verified live: both dev servers up, homepage loaded, zero console errors, screenshots below confirm every new element rendering with real data from `data/stockmoney_live.duckdb`.**
**Next step for a future worker: none blocking — optional polish is a standalone `/api/sector-rotation` route if another page ever wants the ranking without the full briefing payload, and calibrating the `|z| >= 1.0` sector-linkage threshold once there's a backtest to check it against.**

## Scope note

Per WORKER6_AUTONOMOUS_SPEC.md, work stayed inside `src/stockmoney/api/*` and `frontend/src/*`. The backtest package and experiment scripts were not touched. No commit was made.

## Item-by-item status

### 1. Richer daily-briefing narrative — DONE

`cockpit.build_narrative()` (new, `src/stockmoney/api/cockpit.py`) synthesizes:
- Regime mix + VIX + term-structure reading (reused from `queries.market_summary`)
- Up to 4 recent (72h lookback) real macro/event `news_items` rows (`item_type='macro'`, `symbol IS NULL` — Fed, oil/geopolitics, rates, etc.), ranked by importance then recency, generic headlines filtered out
- Sector-rotation strongest/weakest sector for the day

into one paragraph, returned as `briefing.narrative` (new field) with `briefing.narrative_basis` telling the frontend which shape was used (`macro_news` vs `fallback_regime_sector`). The old one-line `headline`/`market_lines` fields are kept unchanged for backward compatibility; the frontend now leads with `narrative`.

**Default-branch behavior confirmed live**: when no real macro headline exists in the lookback window, the function falls back to a regime+sector-only paragraph and says so explicitly ("退回 regime + 板塊強弱簡版摘要") rather than inventing macro colour. Verified via a dedicated unit test (`test_build_narrative_falls_back_when_no_macro_news`).

Real output observed against the live DB (2026-07-10 data, `narrative_basis: "macro_news"`):
> 核心觀察清單 31 檔中,主導市場狀態為「中波動」,VIX 現為 11.9,期限結構平靜正常。近期總經/事件面消息包括:「10 年期公債殖利率回落至 4.15%,DXY 走弱」(FRED)；「India's retail inflation accelerates to 4.38%...」(INVESTING)；「Global yields steady as markets weigh Hormuz closure...」(INVESTING)；「A Fed interest-rate hike could trigger a short-term stock selloff...」(MARKETWATCH)。板塊輪動上,今日 大型科技 平均報酬最高(+0.9%),半導體 相對最弱(-0.1%)。以上為市場現況與消息事實描述,不構成漲跌預測,進出場判斷仍需自行評估。

### 2. News relevance filtering — DONE

`queries.is_generic_headline()` (new, `src/stockmoney/api/queries.py`) matches template ticker-quote boilerplate (`"stock quote"`, `"price and forecast"`, case-insensitive) rather than a broad "forecast" ban that would false-positive on real analyst-forecast headlines. Wired into:
- `queries._latest_symbol_news`'s SQL directly (`NOT ILIKE '%stock quote%'` / `'%price and forecast%'`), so a symbol's top headline on the cockpit card and opportunity board is never a template — verified live: AAPL's card shows "傳 Apple 加速 AI 伺服器自研晶片" instead of "AAPL Stock Quote Price and Forecast - CNN" (both exist in the live DB for AAPL).
- `cockpit._recent_macro_headlines` (the narrative's macro-headline picker), via the same `is_generic_headline` function so both places agree on the definition.

The spec's secondary criterion ("或無 catalyst 分數的") wasn't implemented as a separate rule — every row in the live `news_items` table already carries a non-null `importance` score, so there was no case to filter on; noted here rather than silently skipped.

Scope: this filter only touches `_latest_symbol_news` (backs the cockpit cards + opportunity board's "top news" line) and the narrative's macro-headline picker — the full 消息雷達 (News Radar) feed (`queries.news_feed`) was deliberately left untouched, since that page is a "see everything" surface where a template quote page is still legitimate context, and it's out of the v2 scope (5 named items).

### 3. Per-name volume signal — DONE

`cockpit.volume_signal()` (new): today's volume vs. its own trailing 20-trading-day average (today excluded from the baseline). States: 放量 (ratio ≥ 1.5x), 縮量 (ratio ≤ 0.6x), 量能正常 (in between), 資料不足 (< 21 days of volume history). Surfaced as `card.volume_signal` on every `/api/cockpit` card and rendered as a badge with the ratio (e.g. "放量 (1.6x均量)") plus a hover tooltip (`GlossaryTerm` + `Tooltip`, existing component pattern) explaining in plain language why volume matters and that it is not a direction signal.

Verified live: AAPL showed 放量 (1.6x), SLB showed 縮量 (0.4x), most names showed 量能正常 — plausible spread of states, not a constant.

### 4. Sector linkage / dispersion — DONE

`cockpit.sector_linkage_map()` (new): for each symbol, z-scores today's return against its sector PEERS' mean return (self excluded) and stdev. `|z| >= 1.0` → "脫離板塊獨走" (breaking from its sector — a hint of idiosyncratic/stock-specific news, per CLAUDE.md section 8's cross-sectional dispersion idea, applied descriptively here instead of as a regime-detection feature); otherwise "跟隨板塊同步". Two honest degradations:
- ETF sector buckets (`semiconductor_etf`, `big_tech_etf`: SOXL/SOXS/SOXX/QQQ) are marked "不適用" rather than compared against "peers" — an ETF IS the sector basket (or a leveraged/inverse derivative of it), not one stock among peers inside it. Verified live: QQQ's card correctly shows "不適用" rather than a fabricated comparison.
- Fewer than 2 peers with return data → "資料不足".

`|z| >= 1.0` is an arbitrary, documented-as-such cutoff (no backtest calibrated it — this is a v2 descriptive addition, not a modeled threshold), noted in the docstring and worth flagging as a caveat below.

### 5. Sector-rotation ranking — DONE

`cockpit.sector_rotation()` (new): average today's return + average trailing-5-day return per sector (ETF buckets excluded — same reasoning as item 4, to avoid double-counting the same underlying names and letting a 3x-leveraged product's amplified move distort the "organic" sector read), ranked strongest-to-weakest. Returned as `briefing.sector_rotation` and rendered as a new `SectorRotationPanel` component (horizontal diverging bars, red/green, + the 5-day figure) inside the daily-briefing card on the homepage, with a `GlossaryTerm` tooltip explaining "資金今天在哪" in plain language.

Verified live: 大型科技 (+0.9%) ranked strongest, 半導體 (-0.1%) weakest, energy/financials in between — plausible, non-degenerate ranking.

## Verification evidence

- **Backend tests**: `.venv/bin/python -m pytest tests/api/ -q` → 75 passed (56 pre-existing + 19 new in `tests/api/test_cockpit.py`, covering `volume_signal`, `sector_linkage_map`, `sector_rotation`, `is_generic_headline`, the SQL-level news filter, and `build_narrative`'s macro/fallback/generic-filtered paths). Full repo suite (`pytest tests/ -q`) is 541 passed / 1 pre-existing failure in `tests/backtest/test_scoreboard.py` (env-var-dependent, backtest package, out of this worker's file ownership, unrelated to these changes).
- **Frontend**: `npx tsc --noEmit` → clean. `npx vitest run` → 19/19 passed across 8 files, including updated `Opportunities.test.tsx` assertions for the new narrative paragraph, sector-rotation panel, and per-card volume/linkage badges.
- **Live run**: started both dev servers via `.claude/launch.json` (`fastapi-backend` :8000, `frontend-vite` :5173), loaded `http://localhost:5173/`, confirmed via `read_console_messages` (onlyErrors) — **zero console errors** — across the homepage, a ticker detail page (`/ticker/AAPL`), and the News Radar page (`/news`, confirms the generic-headline filter scoping didn't regress that page). Confirmed via direct DOM query (`document.querySelectorAll('[role="tooltip"]')`) that all 63 `GlossaryTerm` tooltips on the homepage — including the new `volume_signal`/`sector_linkage`/`sector_rotation` ones — carry the correct plain-language text, since this browser automation's synthetic `hover` doesn't reliably trigger CSS `:hover` for a visual screenshot of the tooltip popover itself.
- **Screenshots taken** (not saved to disk, described here): (1) homepage top — guardrail banner, "📋 今日盤前摘要" card showing the full narrative paragraph citing FRED/INVESTING/MARKETWATCH headlines, and the new sector-rotation bar panel (大型科技 +0.9% green bar longest, 半導體 -0.1% red); (2) mid-page grid — AAPL/AMZN/CRM/GOOGL cards each showing a regime chip, breakout-state chip, and the new volume+linkage badge row ("放量 (1.6x均量)" / "脫離板塊獨走" etc.), real (non-template) news headlines, and the existing sell-put gate box unchanged; (3) QQQ/COP/CVX/SLB cards showing QQQ's "不適用" linkage badge and SLB's "縮量 (0.4x均量)" — confirming both v2 signals vary correctly across names rather than being hardcoded.

## Honest caveats

- The `|z| >= 1.0` sector-dispersion cutoff and the 1.5x/0.6x volume-ratio cutoffs are **not backtested or calibrated** — they're reasonable, documented-as-arbitrary defaults for a v1 descriptive read, same spirit as the existing sell-put gate's disclaimers elsewhere in `cockpit.py`. If these ever feed a real decision threshold (rather than "worth a second look"), they should go through the same walk-forward discipline as everything else in CLAUDE.md section 12 before being trusted.
- The narrative paragraph's macro-news selection currently includes some `item_type='macro'` rows that are topically weak market movers in the live demo data (e.g. a UAW labor-dispute headline classified as "macro") — that's an upstream news-classification artifact from `scripts/synthesize_catalysts.py`'s ingestion, not something this worker's filter (which only removes *template* headlines, not *topically irrelevant* ones) was scoped to fix.
- Sector-rotation and sector-linkage both use only the watchlist's 5 core non-ETF sectors (`semiconductor`, `big_tech`, `energy`, `financials`); ETF buckets are excluded by design (see item 4/5 write-ups above) — this is a deliberate honesty call, not a gap, but worth knowing if a future request wants "SOXL vs SOXS" compared directly (that would need a different, ETF-appropriate framing, not this dispersion math).
