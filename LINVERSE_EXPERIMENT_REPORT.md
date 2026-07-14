# "Lin-Inverse" Experiment — Does Momentum Work on High-Vol Names?

**What was done**: Backfilled real daily OHLCV for COIN/MSTR/IREN (+NBIS/CRCL, too short to use), built three price-only option strategies (trend, N-day breakout, sell-put) plus matched `long_stock`/`B2_random` baselines on this SAME universe/dates, and ran the project's permanent scoreboard harness across horizons 2/3/5.

**Verdict**: Unlike all five prior experiments on the megacap/semiconductor universe (where nothing beat `long_stock`), here **trend and breakout beat `long_stock` at horizon=5, and breakout also wins at horizon=3, both statistically significant (95% CI excludes zero)**. This is the first time a directional price-only strategy has cleared that bar in this project.

**Recommended next step**: Treat this as a promising but survivorship-biased lead, not a strategy to trade. Before acting on it: (1) re-run on a *pre-registered* broader high-vol universe chosen by an objective rule (e.g. "top-N by realized vol as of 2019", not "names I already know won") to control for survivorship; (2) check whether the edge is just "never buy puts, always buy calls, on stocks that happened to go up 10-50x" — i.e. decompose how much of the win is pure long-bias vs actual UP/DOWN timing skill; (3) do not skip earnings — the free Nasdaq API only gives ~1 year (4 quarters) of history per symbol, not enough to add an earnings strategy honestly; a paid/deeper source would be needed.

---

## 1. Data coverage (scripts/backfill_highvol_ohlcv.py)

Pulled via yfinance, written to `data/highvol_ohlcv/<SYMBOL>.parquet` (columns: symbol, trade_date, open, high, low, close, adj_close, volume, source — same schema as `ohlcv_daily`). None of these symbols existed in `stockmoney_live.duckdb` beforehand (checked: 0 rows for all five before this run).

| Symbol | Rows | Date span | Notes |
|---|---:|---|---|
| COIN | 1,318 | 2021-04-14 → 2026-07-14 | 5.2y, full IPO history |
| MSTR | 7,065 | 1998-06-11 → 2026-07-14 | 28.1y raw, but **trimmed to 2020-08-11+ for all strategy runs** (see §3) — pre-2020 MSTR was a small enterprise-software company, not the BTC-treasury name Lin trades |
| IREN | 1,166 | 2021-11-17 → 2026-07-14 | 4.7y, full listed history |
| NBIS | 432 | 2024-10-21 → 2026-07-14 | 1.7y — **excluded from the scoreboard**, too short for a meaningful walk-forward split |
| CRCL | 277 | 2025-06-05 → 2026-07-14 | 1.1y — **excluded from the scoreboard**, same reason |

Scoreboard universe: **COIN, MSTR (trimmed), IREN**.

## 2. Scoreboard results (src/stockmoney/backtest/highvol_strategies.py, scripts/highvol_scoreboard_cli.py)

Real output of `.venv/bin/python -m scripts.highvol_scoreboard_cli` (walk-forward OOS by construction of each strategy's own leak-safe loop; option P&L net of theta/bid-ask spread via the shared `build_option_outcomes`/`options_pnl` engine — no pricing/leak-safety logic reimplemented, only new closes-loading + trade-assembly glue). `*` = 95% bootstrap CI excludes zero.

```
HORIZON = 2
strategy                          n    win%   mean_ret    ci_lo    ci_hi     vs baseline
long_stock                     3815   48.3%    +0.0050  +0.0022  +0.0078  - *
B2_random                     95375   37.5%    -0.0413  -0.0441  -0.0383  -
highvol_trend_L20_adx20        2383   40.1%    -0.0023  -0.0248  +0.0191  lose vs long_stock
highvol_breakout_N20            994   42.3%    +0.0175  -0.0157  +0.0523  WIN  vs long_stock
highvol_sellput_otm5_naive     3815   75.9%    +0.0040  +0.0030  +0.0050  lose vs long_stock *

HORIZON = 3
strategy                          n    win%   mean_ret    ci_lo    ci_hi     vs baseline
long_stock                     3812   48.3%    +0.0077  +0.0039  +0.0112  - *
B2_random                     95300   37.1%    -0.0360  -0.0399  -0.0320  -
highvol_trend_L20_adx20        2381   39.0%    +0.0221  -0.0057  +0.0520  WIN  vs long_stock
highvol_breakout_N20            994   42.4%    +0.0520  +0.0096  +0.0961  WIN  vs long_stock *
highvol_sellput_otm5_naive     3812   71.0%    +0.0051  +0.0037  +0.0064  lose vs long_stock *

HORIZON = 5
strategy                          n    win%   mean_ret    ci_lo    ci_hi     vs baseline
long_stock                     3806   48.3%    +0.0131  +0.0084  +0.0175  - *
B2_random                     95150   36.1%    -0.0181  -0.0239  -0.0126  -
highvol_trend_L20_adx20        2377   38.4%    +0.0734  +0.0324  +0.1168  WIN  vs long_stock *
highvol_breakout_N20            994   40.0%    +0.0854  +0.0271  +0.1466  WIN  vs long_stock *
highvol_sellput_otm5_naive     3806   66.7%    +0.0073  +0.0055  +0.0091  lose vs long_stock *
```

**Reading this**:
- `long_stock`'s mean_ret is a *raw price return* (buy-and-hold), while `highvol_trend`/`highvol_breakout`/`highvol_sellput`'s mean_ret is an *option return on premium/collateral* (net of theta and bid-ask spread, via the same pricing engine as every other strategy in this project). Comparing the two is intentional — it answers "did paying premium for a timed option beat just holding the stock" — but the units are not literally the same instrument, so treat "WIN vs long_stock" as "beat the passive floor," not "beat it by that many percentage points on identical capital." This mirrors the pre-existing `SellPutStrategy` vs `long_stock` comparison already in `scoreboard.py`.
- **Trend (L20 momentum + ADX≥20 gate)**: loses at h=2, wins (not significant) at h=3, **wins significantly at h=5** (+7.3% mean vs long_stock's +1.3%, CI [3.2%, 11.7%]).
- **N-day breakout (20-day high/low)**: wins at every horizon, **significant at h=3 and h=5**.
- **Sell-put (naive OTM 5%)**: high win rate (67-76%) as expected for premium-selling, but small/negative-vs-baseline mean return here — unlike the megacap universe, high-vol names' large realized moves regularly blow through the 5% OTM strike, eating the premium collected. Consistent with §4.10's finding that sell-put's edge is fragile and regime-dependent; on genuinely high-vol names it does *not* replicate.
- `B2_random` is soundly negative everywhere (theta/spread tax on unconditional option buying), confirming the harness is behaving as expected.

**This is the first strategy family in the project's five-experiment history to significantly beat `long_stock`.** All four prior experiments (old direction model, SOXL trend, cross-sectional stock-picking, megacap sell-put) failed to clear this bar on the 9-name megacap/semiconductor universe.

## 3. Design notes / what is NOT reimplemented

- Reuses `scoreboard._realized_vol`, `scoreboard._sell_put_trades`, `scoreboard._assemble_direction_trades` (→ `build_option_outcomes`) verbatim — no new pricing or walk-forward logic.
- ADX(14) reuses the project's existing pure function `stockmoney.data.features.adx.wilder_adx` (already unit-tested elsewhere), computed from the parquet's own high/low/close — not reimplemented.
- MSTR trim to 2020-08-11+ (first disclosed Bitcoin purchase) is a **documented, explicit** filter applied only inside `highvol_strategies.py`'s loader, not to the parquet file itself (the parquet keeps MSTR's full real 1998+ history per the backfill task). Without this trim, 22 years of an unrelated small-cap enterprise-software regime would dilute a test of "does momentum work on crypto-concept high-vol names" — that's a different hypothesis than the one asked.
- `long_stock`/`B2_random` are **re-registered under the same names** as `scoreboard.py`'s defaults but scoped to the high-vol universe/parquet, not the DB — deliberately run as their own standalone battery (`scripts/highvol_scoreboard_cli.py`), never mixed into `scoreboard.default_strategies()`, to avoid two different-universe strategies silently colliding under one name in `run_scoreboard`'s internal `mean_by_key` cache.

## 4. Earnings events — SKIPPED, not fabricated

Tried one free source (Nasdaq's public `api.nasdaq.com/api/company/<SYM>/earnings-surprise` JSON endpoint) within the ~15-minute budget. It works and returns real reported-earnings dates (e.g. COIN: 2026-05-07, 2026-02-12, 2025-10-30, 2025-07-31), but **only the trailing 4 fiscal quarters (~1 year) per symbol** — nowhere near the 4-5 year span the price data covers. Pooling ~4 events × 3 symbols = ~12 total earnings dates is too thin to run a leak-safe, statistically meaningful strategy alongside strategies with n in the thousands, so per the task's instruction ("if not obtainable... SKIP it and say so plainly") **no earnings strategy was built**. yfinance's own earnings-date API remains confirmed-blocked (per prior probing). A paid source (or scraping a longer-history endpoint) would be needed to do this properly.

## 5. Caveats (read before acting on §2's numbers)

1. **Survivorship bias — the dominant caveat.** COIN, MSTR, and IREN were chosen precisely because they are today's famous, spectacular winners (COIN/MSTR both up huge on the crypto cycle; IREN on the AI-datacenter pivot). A backtest confined to "assets that already worked out" is optimistically biased by construction — there is no control group of similarly-hyped high-vol names from 2021 that went to zero instead. The true expected value of "trade momentum on whatever looks like the next hot high-vol name, in real time, without hindsight" is materially lower than what §2 shows. Read these numbers as an **upper bound / existence proof that price-only edges CAN exist in this regime**, not as a tradeable expectation.
2. **Short histories.** Even the "usable" universe is thin by this project's usual 8-year standard: COIN/IREN are ~4-5 years, MSTR (post-trim) ~5.9 years. NBIS/CRCL were excluded entirely (1.7y/1.1y) — too short to walk-forward meaningfully.
3. **Trade counts are pooled across only 3 symbols.** n in the scoreboard table (2,000-3,800 per strategy) looks large but is driven by daily overlapping windows on 3 names, not 3,800 independent bets — the effective sample size for a "does this generalize to a new symbol" question is closer to 3.
4. **Win rates are LOW (38-42%) for trend/breakout** — the positive mean return is carried by a small number of very large winning trades (consistent with genuine momentum captured during COIN/MSTR/IREN's biggest multi-week rallies), not a stable high-hit-rate edge. This is a different risk profile than sell-put's high-win-rate/small-size shape found in §4.10, and would need explicit position-sizing/Kelly discipline (CLAUDE.md §9) before any real capital exposure — that discipline was intentionally out of scope for this experiment.
5. **Not grid-searched.** Trend (L20/ADX20) and breakout (N20) parameters are the SAME a priori configs already used elsewhere in this project (not tuned on this universe) — appropriate for an honest first look, but means the reported numbers are not the best this family could plausibly do (nor the worst).
6. **No commit made.** Per instructions, nothing in this experiment was committed to git.

## 6. Files produced

- `scripts/backfill_highvol_ohlcv.py` — yfinance backfill → parquet
- `data/highvol_ohlcv/{COIN,MSTR,IREN,NBIS,CRCL}.parquet` — real OHLCV data
- `src/stockmoney/backtest/highvol_strategies.py` — trend/breakout/sell-put + long_stock/B2_random Strategy plug-ins
- `scripts/highvol_scoreboard_cli.py` — standalone runner
- `LINVERSE_EXPERIMENT_REPORT.md` — this file
