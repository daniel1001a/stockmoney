# Evidence Rate Spec: more trustworthy evidence, faster

Status: approved by owner 2026-10-30 (grilling session). Implementation is split into GitHub issues labelled `evidence`; each issue links back to a section here. Companion: `RULE_MAINTAINER_SPEC.md` (consumes this evidence), `EDGE_PLAN.md` section 5 (paper-to-live gates, unchanged by this spec).

## 1. Problem

The learning loop is starved, not blocked. As of 2026-10-30 the desk has 16 closed paper trades. Every registered hypothesis (H1, H1b, H2, H5, H5b) needs 30 closed trades per side and has 0 to 4. At the current rate a single A/B answer takes months, and the AI maintainer would have nothing to test.

## 2. Owner decisions (locked)

| # | Decision |
|---|---|
| E1 | Historical replay is the primary evidence for screening a change. Forward paper/shadow confirms it. |
| E2 | Every AI proposal gets its own simulated shadow arm on live data. |
| E3 | Wider universe (liquid S&P 500) in **shadow books only**. The paper/live universe (`universe.STOCKS`, 79 names) is unchanged. |
| E4 | New `paper_wide` profile: more slots at a smaller risk per trade, about the same total heat as `paper_aggressive`. Paper only. |
| E5 | Evidence is always labelled by tier and never pooled across tiers: `replay`, `shadow`, `paper`, `live`. CLAUDE.md section 12 (theoretical vs realized reported separately) applies. |

## 3. Replay first (E1)

### 3.1 What exists

- Stocks: `scripts/desk_backtest.py` (daily bars, same strategies, survivorship caveat documented in `universe.py`), `scripts/desk_h1_replay.py`, `scripts/desk_wind_replay.py`.
- Options: `src/stockmoney/desk/options/replay.py`, full-day replay through the same `Engine`, real 1-minute underlying bars, synthetic option quotes (trade bar or Black-Scholes with a modelled half spread), volume filter off.
- Spreads: shadow only (`options/spreads.py`), no replay.

### 3.2 What to build

1. **One replay entry point**: `scripts/desk_replay.py --sleeve {stocks,options,spreads} --from --to --variant <proposal json|incumbent> --out <json>`. It wraps the existing backtest/replay code and returns the same result shape for every sleeve: trades list, avg R per trade, win rate, max drawdown, n, plus `tier: "replay"` and the data caveats in force. The maintainer's evidence pipeline (`RULE_MAINTAINER_SPEC` 7.2 to 7.4) calls only this.
2. **Point-in-time discipline** (CLAUDE.md section 2 and 11):
   - indicators at decision time t use bars with timestamps < t only; a test injects a future bar spike and asserts no decision changes before it;
   - the earnings calendar used in replay is the one stored with its fetch timestamp; when only a later snapshot exists, the result carries caveat `earnings_calendar_not_pit`;
   - Grok/Muse inputs are not replayable (no history before 2026-10). Playbooks that depend on them (`grok_plan`, Grok `in_play`) are marked `not_replayable` and can only be evidenced by shadow.
3. **Spreads replay**: reuse the options replay quote model for both legs (ask for buys, bid for sells, `shadow_slippage_pct`), same entry/exit rules as `spreads.py`. Result labelled `synthetic_quotes`.
4. **Holdout bookkeeping**: replay results on the most recent 12 months are cached by `(variant hash, from, to)`; the maintainer's holdout-once rule reads this cache.
5. **Cost model**: every replay applies slippage + fees from the same config the shadow fill model uses (`options/shadow.py`), and stocks use a fixed bps per side setting (default 5 bps). No zero-cost replay results are stored.

## 4. Shadow arms per proposal (E2)

- Generalize `learn/shadow.py` `VARIANTS` (a static dict today) to also load arms from the `proposals` table: one arm `shadow:P-<id>` for each proposal in `awaiting_owner` or `applied`, plus `shadow:incumbent:P-<id>` after apply (tripwire baseline).
- An arm is the incumbent strategy set with the proposal's single param change or filter applied. Same capital (`CAPITAL = 25_000`), same idempotent per-session `step`.
- Options/spreads arms run inside the intraday shadow engine with the proposal's change applied to a copy of `OptionsConfig`; they write rows tagged with the arm name.
- Cap: `max_active_arms` (default 30). When over cap, the oldest `awaiting_owner` arms past 40 trading days are retired and their proposals marked `expired`.
- Arm results feed `RULE_MAINTAINER_SPEC` 7.7 (shadow agreement) and 10 (tripwire), and sync to Supabase for the Rule Lab.

## 5. Wider universe, shadow only (E3)

- New `universe_wide.py` with a static list of liquid S&P 500 names (filter: 20-day median dollar volume >= $200M, price >= $10), each mapped to a sector ETF in `universe.SECTORS`. Refreshed monthly by a script that writes the list to a versioned JSON in `data/desk/universe_wide/<yyyy-mm>.json`; shadow uses the version in force on each session date (closer to point-in-time membership than today's list).
- New shadow books `shadow:wide:<name>` that mirror `shadow:combined` and each proposal arm but on the wide list. They produce more trades per week for the same rules, which is what the A/B tests need.
- Guard: the order path (`run.py` entry selection) must only ever select from `universe.STOCKS`. A test asserts that no symbol outside `universe.STOCKS` can reach `Broker.submit`, whatever the shadow books hold.
- Data cost: daily bars only for the wide list (no intraday). Fetch once in postclose; the bar store is shared with the existing pipeline.
- Reporting: wide-universe results are labelled `tier: shadow, universe: wide` and are never added to the 79-name results.

## 6. `paper_wide` profile (E4)

Add to `PROFILES` in `src/stockmoney/desk/profiles.py`:

```python
"paper_wide": Profile("paper_wide", 0.009, 20, 4, 0.25, 0.30, 0.40),
```

| Field | paper_aggressive | paper_wide | Reason |
|---|---|---|---|
| risk_per_trade | 1.5% | 0.9% | |
| max_positions | 12 | 20 | more trades per week |
| total heat | 18% | 18% | owner: same total risk |
| max_per_sector | 3 (4.5% sector heat) | 4 (3.6% sector heat) | with 20 slots, a cap of 3 would bind on the tech-heavy list; 4 still lowers sector heat |
| max_position_pct | 0.25 | 0.25 | unchanged |
| max_order_notional_pct | 0.30 | 0.30 | unchanged |
| max_crypto_exposure_pct | 0.40 | 0.40 | unchanged |

- The existing guard in `profiles.active` already refuses every non-`standard` profile unless `ALPACA_ENV=paper`; add a test that covers `paper_wide` explicitly.
- Switching profile is an owner action (`DESK_PROFILE=paper_wide` in `.env`). The switch date is logged in `param_changes` (strategy `desk`, param `profile`) by the first run that sees the new value, so results before and after are never pooled.
- Expected effect: more fills from the same signals that are today skipped for `max_positions`. The postclose report adds one line: "skipped for slots: N this week" so the owner can see whether the change did anything.

## 7. Evidence ledger

One view `evidence_ledger` (SQL view, synced to Supabase) with one row per (variant, tier, window): n trades, avg R, CI95 low/high, max DD, caveats. The digest email, the console and the Rule Lab read only this view, so every number shown says which tier it came from.

## 8. Testing

- Replay: point-in-time future-bar injection test; result shape identical across sleeves; zero-cost results refused; holdout cache returns the stored result for the same hash.
- Shadow arms: proposals table drives arm creation and retirement; cap enforced; idempotent re-run of a session does not double-count.
- Wide universe: order path guard test (section 5); versioned list chosen by session date.
- Profile: `paper_wide` values; refused on `ALPACA_ENV=live`; switch logged once.

## 9. Delivery order (issues)

1. `paper_wide` profile + switch logging + "skipped for slots" line (6). Smallest, starts more trades today.
2. Unified `desk_replay.py` + point-in-time tests + cost model + holdout cache (3.2 items 1, 2, 4, 5).
3. Spreads replay (3.2 item 3).
4. Proposal-driven shadow arms (4). Depends on `proposals` table from `RULE_MAINTAINER_SPEC` slice 4.
5. Wide universe shadow books + order path guard (5).
6. `evidence_ledger` view + sync (7).
