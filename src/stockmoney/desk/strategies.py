"""Trend-following strategies. Each one reads the same three-layer context
(market regime -> sector flow -> stock technicals) and emits Signals whose
``layers`` spell out why, so a human can audit every call in the console.

Adding a strategy = subclass Strategy and add it to REGISTRY. Parameters live in
``params`` so the console can version changes; never tune them on the paper
month's own results without logging the change (see desk/store.py).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from numbers import Real

import pandas as pd

from stockmoney.desk.calendar import is_last_trading_day_of_month
from stockmoney.desk.indicators import Ind, MarketState, sector_ranking
from stockmoney.desk.universe import STOCKS, sector_name


@dataclass
class Signal:
    strategy: str
    symbol: str
    date: pd.Timestamp
    ref_price: float  # close on signal day; the fill happens next open
    stop: float
    score: float
    thesis: str
    invalidation: str
    layers: list[dict] = field(default_factory=list)
    target_weight: float | None = None  # rotation strategies size by weight, not risk


@dataclass
class Context:
    t: pd.Timestamp
    ind: Ind
    market: MarketState
    sectors: pd.DataFrame

    @property
    def leading(self) -> set[str]:
        return set(self.sectors.loc[self.sectors["leading"], "etf"])

    def v(self, frame: str, sym: str) -> float:
        return float(getattr(self.ind, frame).at[self.t, sym])


def build_context(ind: Ind, t: pd.Timestamp, market: MarketState) -> Context:
    return Context(t, ind, market, sector_ranking(ind, t))


def _layer(name: str, ok: bool, note: str) -> dict:
    return {"layer": name, "ok": bool(ok), "note": note}


def _sector_layer(ctx: Context, sym: str) -> dict:
    etf = STOCKS[sym]
    row = ctx.sectors[ctx.sectors["etf"] == etf]
    if row.empty:
        return _layer("資金/板塊", False, f"{sector_name(etf)} 無資料")
    r = row.iloc[0]
    return _layer(
        "資金/板塊",
        etf in ctx.leading,
        f"{sector_name(etf)}({etf}) 63日相對SPY {r.rs63:+.1%},排名 {int(r['rank'])}/{len(ctx.sectors)}",
    )


@dataclass(frozen=True)
class Bound:
    """Declared legal range of one strategy parameter."""

    lo: float | None = None
    hi: float | None = None
    lo_open: bool = False  # True: value must be > lo
    hi_open: bool = False  # True: value must be < hi
    integer: bool = False

    def describe(self) -> str:
        lo = "-inf" if self.lo is None else f"{self.lo:g}"
        hi = "inf" if self.hi is None else f"{self.hi:g}"
        left = "(" if self.lo is None or self.lo_open else "["
        right = ")" if self.hi is None or self.hi_open else "]"
        return f"{left}{lo}, {hi}{right}" + (" integer" if self.integer else "")

    def contains(self, v: float) -> bool:
        if self.lo is not None and (v <= self.lo if self.lo_open else v < self.lo):
            return False
        if self.hi is not None and (v >= self.hi if self.hi_open else v > self.hi):
            return False
        return True


def _ok_signal(s: Signal) -> bool:
    """A signal must carry a finite, positive reference price and stop; a NaN
    ATR would otherwise give a NaN stop and bypass risk-based sizing."""
    return all(isinstance(x, Real) and math.isfinite(x) and x > 0 for x in (s.ref_price, s.stop))


class Strategy:
    name = "base"
    label = ""
    description = ""
    params: dict = {}
    # One declared Bound per parameter (checked in __init__ and by the console).
    bounds: dict[str, Bound] = {}

    def __init__(self, **overrides):
        self.params = type(self).validate_params(overrides)

    @classmethod
    def validate_params(cls, overrides: dict | None = None) -> dict:
        """Defaults merged with ``overrides``, checked against ``bounds``.
        Returns the cleaned dict (ints as int, others as float). Raises
        ValueError naming the offending parameter; never truncates silently."""
        overrides = dict(overrides or {})
        unknown = sorted(set(overrides) - set(cls.params))
        if unknown:
            raise ValueError(f"{cls.name}: unknown parameter(s) {unknown}; allowed: {sorted(cls.params)}")
        out: dict = {}
        for k, default in cls.params.items():
            v = overrides.get(k, default)
            b = cls.bounds.get(k)
            if isinstance(v, bool) or not isinstance(v, Real) or not math.isfinite(float(v)):
                raise ValueError(f"{cls.name}.{k}: {v!r} is not a finite number")
            is_int = isinstance(default, int) or (b is not None and b.integer)
            if is_int:
                if float(v) != math.floor(float(v)):
                    raise ValueError(f"{cls.name}.{k}: {v!r} must be a whole number")
                v = int(v)
            else:
                v = float(v)
            if b is not None and not b.contains(v):
                raise ValueError(f"{cls.name}.{k}: {v!r} outside allowed range {b.describe()}")
            out[k] = v
        cls._cross_check(out)
        return out

    @classmethod
    def _cross_check(cls, params: dict) -> None:
        """Relations between parameters; subclasses override."""

    def candidates(self, ctx: Context) -> list[Signal]:
        """Entry signals for ``ctx.t``; only those with a usable price and stop."""
        return [s for s in self._candidates(ctx) if _ok_signal(s)]

    def _candidates(self, ctx: Context) -> list[Signal]:
        raise NotImplementedError

    def exit_reason(self, ctx: Context, sym: str, pos) -> str | None:
        """Rule-based exit evaluated at close (filled next open). Hard stops are
        handled by the broker intraday."""
        p = self.params
        c = ctx.v("close", sym)
        if c < ctx.v("sma50", sym):
            return "跌破50日均線(趨勢失效)"
        if p.get("max_hold_days") and pos.days_held(ctx.t) >= p["max_hold_days"]:
            return f"持有達 {p['max_hold_days']} 個交易日上限"
        return None

    def trail_stop(self, ctx: Context, sym: str, pos) -> float:
        """Chandelier stop: highest close since entry minus k ATR; never lowered."""
        k = self.params.get("trail_atr", 3.0)
        return max(pos.stop, pos.highest_close - k * ctx.v("atr20", sym))


class Breakout(Strategy):
    name = "breakout"
    label = "強勢突破"
    description = (
        "大盤非空頭(多頭順風或中性皆可進場;僅空頭時停手,中性時總曝險上限降為 50%)"
        " + 領漲板塊 + 個股多頭排列,收盤突破55日新高且放量。"
    )
    params = {"stop_atr": 2.5, "trail_atr": 3.0, "min_vol_ratio": 1.3, "max_hold_days": 60}
    bounds = {
        "stop_atr": Bound(0.5, 10, lo_open=True),
        "trail_atr": Bound(0.5, 10, lo_open=True),
        "min_vol_ratio": Bound(0.5, 10),
        "max_hold_days": Bound(5, 400, integer=True),
    }

    def _candidates(self, ctx: Context) -> list[Signal]:
        p, out = self.params, []
        if ctx.market.regime == "risk_off":
            return out
        for sym in STOCKS:
            if sym not in ctx.ind.close or pd.isna(ctx.v("sma200", sym)) or pd.isna(ctx.v("prior_high55", sym)):
                continue
            c, s50, s200, a = (ctx.v(f, sym) for f in ("close", "sma50", "sma200", "atr20"))
            trend_ok = c > s50 > s200
            brk = c > ctx.v("prior_high55", sym)
            vr = ctx.v("vol_ratio", sym)
            if not (trend_ok and brk and vr >= p["min_vol_ratio"]):
                continue
            sec = _sector_layer(ctx, sym)
            if not sec["ok"]:
                continue
            stop = c - p["stop_atr"] * a
            out.append(
                Signal(
                    self.name, sym, ctx.t, c, stop,
                    score=ctx.v("rs63_vs_spy", sym),
                    thesis=f"{sym} 在領漲板塊中放量創55日新高,順勢追強。",
                    invalidation=f"收盤跌破 {stop:.2f}(-{p['stop_atr']}ATR)或跌破50日線",
                    layers=[
                        _layer("大盤", True, ctx.market.note),
                        sec,
                        _layer("個股趨勢", trend_ok, f"收 {c:.2f} > 50MA {s50:.2f} > 200MA {s200:.2f}"),
                        _layer("觸發", True, f"突破55日高 {ctx.v('prior_high55', sym):.2f},量比 {vr:.1f}x"),
                    ],
                )
            )
        return out


class Pullback(Strategy):
    name = "pullback"
    label = "多頭回檔"
    description = "大盤多頭 + 相對強勢股,回測20日均線後轉強收紅,低風險上車。"
    params = {"trail_atr": 3.0, "max_stop_atr": 3.0, "rs_quantile": 0.7, "max_hold_days": 40}
    bounds = {
        "trail_atr": Bound(0.5, 10, lo_open=True),
        "max_stop_atr": Bound(0.5, 10, lo_open=True),
        "rs_quantile": Bound(0, 1, lo_open=True, hi_open=True),
        "max_hold_days": Bound(5, 400, integer=True),
    }

    def _candidates(self, ctx: Context) -> list[Signal]:
        p, out = self.params, []
        if ctx.market.regime != "risk_on":
            return out
        stocks = [s for s in STOCKS if s in ctx.ind.close]
        rs = ctx.ind.rs63_vs_spy.loc[ctx.t, stocks].dropna()
        if rs.empty:
            return out
        cutoff = rs.quantile(p["rs_quantile"])
        prev = ctx.ind.close.index.get_loc(ctx.t) - 1
        if prev < 0:
            return out
        for sym in rs[rs >= cutoff].index:
            c, s50, s200, e20, a = (ctx.v(f, sym) for f in ("close", "sma50", "sma200", "ema20", "atr20"))
            if pd.isna(s200) or not (c > s50 > s200):
                continue
            prev_close = float(ctx.ind.close[sym].iloc[prev])
            low10 = ctx.v("low10", sym)
            touched = low10 <= e20 * 1.01 and ctx.v("ret21", sym) < 0.08
            reclaim = c > e20 and c > prev_close
            if not (touched and reclaim):
                continue
            stop = max(low10 - 0.5 * a, c - p["max_stop_atr"] * a)
            out.append(
                Signal(
                    self.name, sym, ctx.t, c, stop,
                    score=float(rs[sym]),
                    thesis=f"{sym} 為相對強勢股(63日勝SPY {rs[sym]:+.1%}),回測20日線後轉強。",
                    invalidation=f"跌破回檔低點附近 {stop:.2f} 或跌破50日線",
                    layers=[
                        _layer("大盤", True, ctx.market.note),
                        _sector_layer(ctx, sym),
                        _layer("個股趨勢", True, f"收 {c:.2f} > 50MA {s50:.2f} > 200MA {s200:.2f},RS 前{1-p['rs_quantile']:.0%}"),
                        _layer("觸發", True, f"10日低 {low10:.2f} 觸及20EMA {e20:.2f},今日收紅站回"),
                    ],
                )
            )
        return out


class Rotation(Strategy):
    """Monthly relative-momentum rotation: hold the strongest names while the
    market trend is up. Sizes by equal weight rather than stop distance."""

    name = "rotation"
    label = "動能輪動"
    description = "每月第一個交易日,持有 12-1 月動能最強且站上200日線的前5檔;大盤轉空即清倉。"
    params = {"top_n": 5, "keep_rank": 10, "trail_atr": 4.0, "stop_atr": 4.0}
    bounds = {
        "top_n": Bound(1, 20, integer=True),
        "keep_rank": Bound(1, None, integer=True),  # and >= top_n, see _cross_check
        "trail_atr": Bound(0.5, 10, lo_open=True),
        "stop_atr": Bound(0.5, 10, lo_open=True),
    }

    @classmethod
    def _cross_check(cls, params: dict) -> None:
        if params["keep_rank"] < params["top_n"]:
            raise ValueError(
                f"{cls.name}.keep_rank: {params['keep_rank']} must be >= top_n ({params['top_n']})"
            )

    def _ranked(self, ctx: Context) -> pd.Series:
        stocks = [s for s in STOCKS if s in ctx.ind.close]
        m = ctx.ind.mom_12_1.loc[ctx.t, stocks] / ctx.ind.vol63.loc[ctx.t, stocks]
        above = ctx.ind.close.loc[ctx.t, stocks] > ctx.ind.sma200.loc[ctx.t, stocks]
        return m[above].dropna().sort_values(ascending=False)

    @staticmethod
    def _rebalance_day(ctx: Context) -> bool:
        """Last NYSE session of the month. Decided from the exchange calendar
        alone, so a live run (no future bar) and a backtest cannot disagree."""
        return is_last_trading_day_of_month(ctx.t)

    def _candidates(self, ctx: Context) -> list[Signal]:
        p = self.params
        if ctx.market.regime == "risk_off" or not self._rebalance_day(ctx):
            return []
        ranked = self._ranked(ctx).head(p["top_n"])
        out = []
        for rank, (sym, score) in enumerate(ranked.items(), 1):
            c, a = ctx.v("close", sym), ctx.v("atr20", sym)
            out.append(
                Signal(
                    self.name, sym, ctx.t, c, c - p["stop_atr"] * a, float(score),
                    thesis=f"{sym} 動能排名第 {rank}(風險調整後 12-1 月報酬)。",
                    invalidation="月底掉出前10名、跌破200日線、或大盤轉空",
                    layers=[
                        _layer("大盤", True, ctx.market.note),
                        _sector_layer(ctx, sym),
                        _layer("個股動能", True, f"12-1月報酬 {ctx.v('mom_12_1', sym):+.1%},波動 {ctx.v('vol63', sym):.0%}"),
                    ],
                    target_weight=1.0 / p["top_n"],
                )
            )
        return out

    def exit_reason(self, ctx: Context, sym: str, pos) -> str | None:
        if ctx.market.regime == "risk_off":
            return "大盤轉空,輪動策略清倉"
        if ctx.v("close", sym) < ctx.v("sma200", sym):
            return "跌破200日線"
        if self._rebalance_day(ctx):
            ranked = list(self._ranked(ctx).index[: self.params["keep_rank"]])
            if sym not in ranked:
                return f"月度再平衡:掉出前{self.params['keep_rank']}名"
        return None


REGISTRY: dict[str, type[Strategy]] = {s.name: s for s in (Breakout, Pullback, Rotation)}
