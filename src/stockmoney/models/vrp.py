"""Market-level volatility risk premium (VRP) target — IMPROVEMENT_PLAN.md §S2.

Design note (this module is Wave C's "look-ahead HIGH RISK, plan first" item
per NEXT_AGENT_PLAN.md; this docstring is that short plan, in the same spot
backtest_options_pnl.py / options_pnl.py already keep theirs):

Module A predicts *direction*. It can be right on direction and still lose
money on an option bought at rich IV that gets crushed by theta (CLAUDE.md
§0/§6's whole reason for a separate EV gate). VRP asks a different, options-
native question: is the market's priced-in (implied) volatility over- or
under-stating how much the underlying will actually move?

    VRP(d) = forward_realized_vol(d, d+horizon) - entry_iv(d)

Positive VRP -> the underlying will realize MORE vol than priced -> long
premium (straddle/directional long) has an edge. Negative VRP -> IV is rich
relative to what actually happens -> selling premium has an edge. This is the
textbook, well-documented (variance risk premium literature) reason IV
usually trades a bit above realized vol.

**Market-level, not per-symbol, and why:** per-symbol VRP needs a real IV
history per watchlist name, which only exists once Agent 3's daily
iv_surface_daily capture cron (running since 2026-07-12) has accumulated
enough days — there is no way to backtest that yet. VIX, by contrast, is
CBOE's own 30-day implied vol of the S&P 500 with full free multi-year daily
history (ingestion/vix_term.py) — a genuinely backtestable IV series today.
Its natural realized-vol counterpart is the S&P 500 itself, proxied here by
SPY (ingestion/market_index.py, a dedicated table — see 036's migration
comment for why it's NOT merged into ohlcv_daily).

**Look-ahead discipline** (the actual risk this module has to get right):
- `entry_iv(d)` is VIX's OWN trade_date=d close, stamped `available_at` =
  that row's `ingested_at` — a same-day-close value, exactly the convention
  feature_matrix.py already uses for realized_vol_20d/adx_14 (computed AS OF
  d's close, available same day). Never a future VIX print.
- `forward_realized_vol(d, ...)` uses ONLY SPY closes strictly AFTER d — index
  i+1..i+horizon in the trading-day sequence, d itself (index i) is excluded
  from the return window entirely (mirrors feature_matrix.py's fwd_return,
  which anchors at c0=closes[i] and only differences against the future).
  This is the target label, deliberately forward-looking, and — like
  feature_matrix's label — never fed back in as a feature.
- Same annualization convention as realized_vol_20d (log returns, sample
  variance, *sqrt(252)) so `vrp` is a like-for-like subtraction of two
  annualized-vol numbers, not an apples-to-oranges mismatch.
- Resolved (`build_vrp_matrix`) vs unresolved (`latest_unresolved_vrp_row`)
  rows are disjoint and exhaustive over trade_date, verified in
  tests/models/test_vrp.py exactly like feature_matrix's own disjointness
  test — a trade_date is resolved iff its full forward window already exists
  in market_index_ohlcv_daily.
- tests/models/test_leakage_canary.py's "append future rows, past values must
  not change" canary is extended here too (test_vrp.py): appending future
  SPY/VIX rows must not alter any already-resolved historical VRP row.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime

import duckdb
import polars as pl

TRADING_DAYS = 252
MARKET_INDEX_SYMBOL = "SPY"
VIX_TENOR_DAYS = 30
# ~30 calendar days at ~5 trading days/week, matching VIX's 30-calendar-day
# quoting convention so entry_iv and the forward realized-vol window cover
# comparable spans.
DEFAULT_HORIZON = 21


@dataclass
class VrpDataset:
    trade_dates: list[date]
    available_at: list[datetime]
    label_end_dates: list[date]
    entry_iv: "list[float]"          # VIX_TENOR_DAYS-day VIX / 100
    forward_realized_vol: "list[float]"
    vrp: "list[float]"               # forward_realized_vol - entry_iv
    entry_spot: "list[float]"        # SPY close at trade_date, for the options backtest


def _load_market_closes(conn: duckdb.DuckDBPyConnection, symbol: str) -> list[tuple[date, float, datetime]]:
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT trade_date, close, ingested_at,
                   row_number() OVER (
                       PARTITION BY trade_date ORDER BY ingested_at DESC
                   ) AS rn
            FROM market_index_ohlcv_daily
            WHERE symbol = ? AND close IS NOT NULL
        )
        SELECT trade_date, close, ingested_at FROM latest WHERE rn = 1 ORDER BY trade_date
        """,
        [symbol],
    ).fetchall()
    return [(d, c, ia) for d, c, ia in rows]


def _load_vix(conn: duckdb.DuckDBPyConnection, tenor_days: int) -> dict[date, tuple[float, datetime]]:
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT trade_date, vix_value, ingested_at,
                   row_number() OVER (
                       PARTITION BY trade_date ORDER BY ingested_at DESC
                   ) AS rn
            FROM vix_term_structure_daily
            WHERE tenor_days = ? AND vix_value IS NOT NULL
        )
        SELECT trade_date, vix_value, ingested_at FROM latest WHERE rn = 1
        """,
        [tenor_days],
    ).fetchall()
    return {d: (v, ia) for d, v, ia in rows}


def _annualized_vol(log_returns: list[float]) -> float | None:
    n = len(log_returns)
    if n < 2:
        return None
    mean = sum(log_returns) / n
    variance = sum((r - mean) ** 2 for r in log_returns) / (n - 1)
    return math.sqrt(variance * TRADING_DAYS)


def build_vrp_matrix(
    conn: duckdb.DuckDBPyConnection,
    *,
    index_symbol: str = MARKET_INDEX_SYMBOL,
    vix_tenor_days: int = VIX_TENOR_DAYS,
    horizon: int = DEFAULT_HORIZON,
) -> pl.DataFrame:
    """One row per trade_date whose full forward realized-vol window already
    exists (the "resolved" set) — see latest_unresolved_vrp_row for the
    complement. Columns: trade_date, available_at, label_end_date, entry_iv,
    forward_realized_vol, vrp, fwd_return, entry_spot."""
    closes = _load_market_closes(conn, index_symbol)
    vix = _load_vix(conn, vix_tenor_days)

    rows = []
    for i, (d, c0, ingested_at) in enumerate(closes):
        if d not in vix:
            continue
        if i + horizon >= len(closes):  # no full forward window yet
            continue

        vix_value, vix_ingested_at = vix[d]
        entry_iv = vix_value / 100.0

        log_returns = [
            math.log(closes[j][1] / closes[j - 1][1])
            for j in range(i + 1, i + horizon + 1)
            if closes[j - 1][1] > 0 and closes[j][1] > 0
        ]
        fwd_vol = _annualized_vol(log_returns)
        if fwd_vol is None:
            continue

        cN = closes[i + horizon][1]
        fwd_return = cN / c0 - 1.0 if c0 > 0 else None

        rows.append(
            {
                "trade_date": d,
                "available_at": max(ingested_at, vix_ingested_at),
                "label_end_date": closes[i + horizon][0],
                "entry_iv": entry_iv,
                "forward_realized_vol": fwd_vol,
                "vrp": fwd_vol - entry_iv,
                "fwd_return": fwd_return,
                "entry_spot": c0,
            }
        )

    schema = {
        "trade_date": pl.Date, "available_at": pl.Datetime, "label_end_date": pl.Date,
        "entry_iv": pl.Float64, "forward_realized_vol": pl.Float64, "vrp": pl.Float64,
        "fwd_return": pl.Float64, "entry_spot": pl.Float64,
    }
    if not rows:
        return pl.DataFrame(schema=schema)
    return pl.DataFrame(rows, schema=schema).sort("trade_date")


def latest_unresolved_vrp_row(
    conn: duckdb.DuckDBPyConnection,
    *,
    index_symbol: str = MARKET_INDEX_SYMBOL,
    vix_tenor_days: int = VIX_TENOR_DAYS,
    horizon: int = DEFAULT_HORIZON,
) -> dict | None:
    """The most recent trade_date with a known entry_iv but no resolved
    forward_realized_vol yet -- the exact negation of build_vrp_matrix's
    `i + horizon >= len(closes)` cutoff, mirroring
    feature_matrix.latest_unresolved_feature_rows. Returns None if there's no
    such row (e.g. VIX/SPY data hasn't been backfilled)."""
    closes = _load_market_closes(conn, index_symbol)
    vix = _load_vix(conn, vix_tenor_days)

    for i in range(len(closes) - 1, -1, -1):
        d, c0, ingested_at = closes[i]
        if d not in vix:
            continue
        if i + horizon < len(closes):  # already resolved
            return None
        vix_value, vix_ingested_at = vix[d]
        return {
            "trade_date": d,
            "available_at": max(ingested_at, vix_ingested_at),
            "entry_iv": vix_value / 100.0,
            "entry_spot": c0,
        }
    return None
