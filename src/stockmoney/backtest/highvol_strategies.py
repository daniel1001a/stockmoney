""""Lin-inverse" experiment: does ANY price-only strategy (trend, breakout,
sell-put) beat plain buy-and-hold on Lin's actual trading universe -- the
high-volatility / crypto-concept momentum names (COIN, MSTR, IREN) -- where
five prior experiments (REBUILD_PLAN.md sec.4.6-4.12, phase0-timemachine-
verdict memory) already showed NOTHING beats `long_stock` on the 9-name
megacap/semiconductor universe?

This module does NOT modify `stockmoney.backtest.scoreboard`'s core, exactly
like `news_strategies.py`: it only imports scoreboard's already leak-tested
glue (`_realized_vol`, `_sma`, `_assemble_direction_trades`, `_sell_put_trades`)
and registers new `Strategy`-protocol plug-ins, per scoreboard.py's module
docstring ("Anyone can define a new class satisfying [the Strategy protocol]
... nothing in this module needs to change").

Data source: this universe is NOT in stockmoney_live.duckdb (confirmed by
querying `ohlcv_daily` before writing this module -- 0 rows for
COIN/MSTR/IREN/NBIS/CRCL). Closes are loaded instead from the parquet written
by `scripts/backfill_highvol_ohlcv.py` (data/highvol_ohlcv/<SYMBOL>.parquet),
via `_load_highvol_closes` below -- deliberately NOT going through
`ScoreboardContext.closes()` (which only knows how to query the DB). Every
strategy here still takes `(ctx: ScoreboardContext, horizon: int)` to satisfy
the `Strategy` protocol so it plugs into `run_scoreboard` unmodified, but
internally ignores `ctx.conn` and reads the module-level parquet cache
instead.

MSTR TRIM (documented, not silent): MSTR's yfinance history starts 1998-06-11,
but it was a boring small-cap enterprise-software company until its Bitcoin-
treasury pivot (first BTC purchase announced 2020-08-11). Testing "does
momentum work on crypto-concept high-vol names" against 22 years of an
unrelated micro-cap regime would not be a fair test of the stated hypothesis,
so MSTR closes are trimmed to `MSTR_TRIM_START` (2020-08-11) onward for every
strategy in this module. COIN and IREN are used in full (COIN IPO'd already
as the crypto-exchange it is; IREN's full listed history is short and already
in-thesis). This trim is applied ONLY here, not to the parquet file itself
(the parquet keeps MSTR's full real history per the backfill task's
instructions).

SURVIVORSHIP CAVEAT (repeated from LINVERSE_EXPERIMENT_REPORT.md -- read it
too): COIN/MSTR/IREN are today's famous winners. A backtest confined to
"stocks that are famous winners today" is optimistically biased by
construction; any positive result here should be read as an upper bound, not
an expectation.
"""
from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import numpy as np
import polars as pl

from stockmoney.backtest.scoreboard import (
    ScoreboardContext,
    TradeResult,
    _assemble_direction_trades,
    _realized_vol,
    _sell_put_trades,
)
from stockmoney.data.features.adx import wilder_adx
from stockmoney.models.backtest_options_pnl import IV_RV_RATIO
from stockmoney.models.feature_matrix import DOWN, UP

HIGHVOL_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "highvol_ohlcv"

HIGHVOL_UNIVERSE = ["COIN", "MSTR", "IREN"]
MSTR_TRIM_START = date(2020, 8, 11)  # first disclosed BTC purchase

TREND_LOOKBACK = 20
TREND_ADX_MIN = 20.0  # same a priori canonical config as scoreboard.TrendCanonStrategy
VOL_WIN = 20

BREAKOUT_LOOKBACK = 20  # N-day high/low breakout

SELLPUT_OTM = 0.05  # matches scoreboard's naive OTM% for direct comparability

B2_N_SEEDS = 25

_HIGHVOL_CACHE: dict[str, tuple[list[date], np.ndarray, np.ndarray, np.ndarray]] = {}


def _load_highvol_closes(symbol: str) -> tuple[list[date], np.ndarray, np.ndarray, np.ndarray]:
    """Returns (trade_dates, close, high, low) for `symbol`, loaded from the
    parquet backfilled by scripts/backfill_highvol_ohlcv.py, sorted by date,
    with the documented MSTR trim applied. Cached at module level (mirrors
    ScoreboardContext.closes()'s own caching, but keyed on parquet instead of
    the DB since this universe isn't in ohlcv_daily)."""
    if symbol not in _HIGHVOL_CACHE:
        path = HIGHVOL_DIR / f"{symbol}.parquet"
        if not path.exists():
            raise FileNotFoundError(
                f"{path} missing -- run scripts/backfill_highvol_ohlcv.py first"
            )
        df = pl.read_parquet(path).sort("trade_date")
        if symbol == "MSTR":
            df = df.filter(pl.col("trade_date") >= MSTR_TRIM_START)
        dts = df["trade_date"].to_list()
        close = df["close"].to_numpy().astype(float)
        high = df["high"].to_numpy().astype(float)
        low = df["low"].to_numpy().astype(float)
        _HIGHVOL_CACHE[symbol] = (dts, close, high, low)
    return _HIGHVOL_CACHE[symbol]


def _adx_by_date(symbol: str) -> dict[date, float]:
    dts, close, high, low = _load_highvol_closes(symbol)
    out: dict[date, float] = {}
    for idx, adx in wilder_adx(high.tolist(), low.tolist(), close.tolist(), period=14):
        out[dts[idx]] = adx
    return out


# =============================================================================
# 1) Trend / momentum (L20 momentum, ADX>=20 regime gate) -- the SAME rule
#    that lost to naked-long on the megacap/SOXL universe (REBUILD_PLAN sec.
#    4.7). Tests whether it fares differently on high-vol momentum names.
# =============================================================================


class HighvolTrendStrategy:
    name = f"highvol_trend_L{TREND_LOOKBACK}_adx{int(TREND_ADX_MIN)}"
    baseline = "long_stock"

    def run(self, ctx: ScoreboardContext, horizon: int) -> list[TradeResult]:
        raw: list[tuple[date, date, int, float, float, float]] = []
        for sym in HIGHVOL_UNIVERSE:
            dts, cl, high, low = _load_highvol_closes(sym)
            adx_by_date = _adx_by_date(sym)
            n = len(cl)
            start = max(TREND_LOOKBACK, VOL_WIN, 28)  # 28 = 2*adx period, ADX needs warmup
            for i in range(start, n - horizon):
                if adx_by_date.get(dts[i], 0.0) < TREND_ADX_MIN:
                    continue  # chop regime -> sit out, same discipline as scoreboard's TREND
                mom = cl[i] / cl[i - TREND_LOOKBACK] - 1.0
                if mom == 0.0:
                    continue
                direction = UP if mom > 0 else DOWN
                rv = _realized_vol(cl, i, VOL_WIN)
                if not math.isfinite(rv) or rv <= 0:
                    continue
                fwd = float(cl[i + horizon] / cl[i] - 1.0)
                raw.append((dts[i], dts[i + horizon], direction, fwd, float(cl[i]), rv * IV_RV_RATIO))
        return _assemble_direction_trades(raw)


# =============================================================================
# 2) N-day high/low breakout -> directional option
# =============================================================================


class HighvolBreakoutStrategy:
    name = f"highvol_breakout_N{BREAKOUT_LOOKBACK}"
    baseline = "long_stock"

    def run(self, ctx: ScoreboardContext, horizon: int) -> list[TradeResult]:
        raw: list[tuple[date, date, int, float, float, float]] = []
        for sym in HIGHVOL_UNIVERSE:
            dts, cl, high, low = _load_highvol_closes(sym)
            n = len(cl)
            start = max(BREAKOUT_LOOKBACK, VOL_WIN)
            for i in range(start, n - horizon):
                prior_high = cl[i - BREAKOUT_LOOKBACK:i].max()
                prior_low = cl[i - BREAKOUT_LOOKBACK:i].min()
                if cl[i] > prior_high:
                    direction = UP
                elif cl[i] < prior_low:
                    direction = DOWN
                else:
                    continue  # inside the N-day range -> no breakout, no trade
                rv = _realized_vol(cl, i, VOL_WIN)
                if not math.isfinite(rv) or rv <= 0:
                    continue
                fwd = float(cl[i + horizon] / cl[i] - 1.0)
                raw.append((dts[i], dts[i + horizon], direction, fwd, float(cl[i]), rv * IV_RV_RATIO))
        return _assemble_direction_trades(raw)


# =============================================================================
# 3) Sell-put (naive, cash-secured OTM put) -- the project's only prior
#    significant-positive strategy (REBUILD_PLAN sec.4.10), tested here on
#    the SAME high-vol universe as everything else in this module.
# =============================================================================


class HighvolSellPutStrategy:
    name = f"highvol_sellput_otm{int(round(SELLPUT_OTM * 100))}_naive"
    baseline = "long_stock"

    def run(self, ctx: ScoreboardContext, horizon: int) -> list[TradeResult]:
        out: list[TradeResult] = []
        for sym in HIGHVOL_UNIVERSE:
            dts, cl, high, low = _load_highvol_closes(sym)
            for td, led, collateral_return, won in _sell_put_trades(dts, cl, SELLPUT_OTM, horizon, gate=False):
                out.append(TradeResult(td, led, collateral_return, won, None))
        return out


# =============================================================================
# Baselines, computed on the SAME high-vol universe + dates so every
# comparison above is apples-to-apples (spec requirement).
# =============================================================================


class HighvolLongStockStrategy:
    """Baseline: plain buy-and-hold over the high-vol universe/dates -- same
    role as scoreboard.LongStockStrategy, but recomputed here on this
    module's own (parquet-backed, MSTR-trimmed) universe rather than reusing
    the DB-backed one, since the symbol sets differ."""

    name = "long_stock"
    baseline = None

    def run(self, ctx: ScoreboardContext, horizon: int) -> list[TradeResult]:
        out: list[TradeResult] = []
        start = max(VOL_WIN, 50)
        for sym in HIGHVOL_UNIVERSE:
            dts, cl, high, low = _load_highvol_closes(sym)
            n = len(cl)
            for i in range(start, n - horizon):
                ret = float(cl[i + horizon] / cl[i] - 1.0)
                out.append(TradeResult(dts[i], dts[i + horizon], ret, ret > 0, None))
        return out


class HighvolB2RandomStrategy:
    """Baseline: buy a call-or-put at random each day, pooled over many
    seeds, on the SAME high-vol universe/dates -- the "random short-dated
    option" floor, same role as scoreboard.B2RandomStrategy."""

    name = "B2_random"
    baseline = None

    def run(self, ctx: ScoreboardContext, horizon: int) -> list[TradeResult]:
        raw_by_symbol: dict[str, list[tuple[date, date, float, float]]] = {}
        for sym in HIGHVOL_UNIVERSE:
            dts, cl, high, low = _load_highvol_closes(sym)
            n = len(cl)
            start = max(VOL_WIN, 50)
            rows = []
            for i in range(start, n - horizon):
                rv = _realized_vol(cl, i, VOL_WIN)
                if not math.isfinite(rv) or rv <= 0:
                    continue
                fwd = float(cl[i + horizon] / cl[i] - 1.0)
                rows.append((dts[i], dts[i + horizon], float(cl[i]), rv * IV_RV_RATIO, fwd))
            raw_by_symbol[sym] = rows

        out: list[TradeResult] = []
        for s in range(B2_N_SEEDS):
            rng = np.random.default_rng(1000 + s)
            raw: list[tuple[date, date, int, float, float, float]] = []
            for sym, rows in raw_by_symbol.items():
                if not rows:
                    continue
                picks = rng.choice([DOWN, UP], size=len(rows))
                for (td, led, spot, iv, fwd), direction in zip(rows, picks):
                    raw.append((td, led, int(direction), fwd, spot, iv))
            out.extend(_assemble_direction_trades(raw))
        return out


def highvol_strategies() -> list:
    """Standalone battery for the high-vol universe. Run this alone (NOT
    mixed into scoreboard.default_strategies()) since it deliberately reuses
    the names `long_stock` / `B2_random` scoped to a different universe --
    see scripts/highvol_scoreboard_cli.py."""
    return [
        HighvolLongStockStrategy(),
        HighvolB2RandomStrategy(),
        HighvolTrendStrategy(),
        HighvolBreakoutStrategy(),
        HighvolSellPutStrategy(),
    ]
