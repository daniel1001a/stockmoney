"""Causal indicator frames. Every value at row t uses only rows <= t, so the
engine can read row t on day t without look-ahead
(tests/desk/test_desk.py::test_indicators_have_no_lookahead checks this by
recomputing on truncated data)."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from stockmoney.desk.data import Bars
from stockmoney.desk.universe import MARKET, SECTORS, STOCKS


def atr(b: Bars, n: int = 20) -> pd.DataFrame:
    prev = b.close.shift(1)
    tr = np.maximum(b.high - b.low, np.maximum((b.high - prev).abs(), (b.low - prev).abs()))
    return tr.rolling(n, min_periods=n).mean()


@dataclass
class Ind:
    close: pd.DataFrame
    sma20: pd.DataFrame
    sma50: pd.DataFrame
    sma200: pd.DataFrame
    ema20: pd.DataFrame
    atr20: pd.DataFrame
    prior_high55: pd.DataFrame  # highest close of the 55 sessions BEFORE today
    low10: pd.DataFrame
    vol_ratio: pd.DataFrame  # today's volume / 50d average
    ret21: pd.DataFrame
    ret63: pd.DataFrame
    mom_12_1: pd.DataFrame  # 252d return skipping the latest 21d
    vol63: pd.DataFrame  # annualised realised vol
    rs63_vs_spy: pd.DataFrame
    rs63_vs_sector: pd.DataFrame


def compute(b: Bars) -> Ind:
    c = b.close
    ret63 = c / c.shift(63) - 1
    spy63 = ret63[MARKET]
    rs_spy = ret63.sub(spy63, axis=0)
    rs_sector = pd.DataFrame(np.nan, index=c.index, columns=c.columns)
    for sym, etf in STOCKS.items():
        if sym in c and etf in c:
            rs_sector[sym] = ret63[sym] - ret63[etf]
    return Ind(
        close=c,
        sma20=c.rolling(20, min_periods=20).mean(),
        sma50=c.rolling(50, min_periods=50).mean(),
        sma200=c.rolling(200, min_periods=200).mean(),
        ema20=c.ewm(span=20, adjust=False, min_periods=20).mean(),
        atr20=atr(b),
        prior_high55=c.shift(1).rolling(55, min_periods=55).max(),
        low10=b.low.rolling(10, min_periods=10).min(),
        vol_ratio=b.volume / b.volume.rolling(50, min_periods=50).mean(),
        ret21=c / c.shift(21) - 1,
        ret63=ret63,
        mom_12_1=c.shift(21) / c.shift(252) - 1,
        vol63=np.log(c).diff().rolling(63, min_periods=63).std() * np.sqrt(252),
        rs63_vs_spy=rs_spy,
        rs63_vs_sector=rs_sector,
    )


# ---------------------------------------------------------------- market layer

@dataclass
class MarketState:
    regime: str  # risk_on | neutral | risk_off
    exposure_cap: float  # max fraction of equity invested
    breadth: float  # share of stocks above their 50d SMA
    note: str


REGIME_LABEL = {"risk_on": "多頭順風", "neutral": "中性/轉折", "risk_off": "空頭逆風"}


def market_state(ind: Ind, t: pd.Timestamp) -> MarketState:
    c, s50, s200 = ind.close.at[t, MARKET], ind.sma50.at[t, MARKET], ind.sma200.at[t, MARKET]
    stocks = [s for s in STOCKS if s in ind.close]
    above = (ind.close.loc[t, stocks] > ind.sma50.loc[t, stocks])
    valid = ind.sma50.loc[t, stocks].notna()
    breadth = float(above[valid].mean()) if valid.any() else float("nan")
    if c > s200 and s50 > s200:
        regime = "risk_on"
    elif c > s200 or s50 > s200:
        regime = "neutral"
    else:
        regime = "risk_off"
    # weak participation under a rising index = narrow, fragile rally
    if regime == "risk_on" and breadth < 0.40:
        regime = "neutral"
    cap = {"risk_on": 1.0, "neutral": 0.5, "risk_off": 0.0}[regime]
    note = (
        f"SPY {c:.2f} / 50MA {s50:.2f} / 200MA {s200:.2f};"
        f" 站上50MA個股 {breadth:.0%} -> {REGIME_LABEL[regime]}(總曝險上限 {cap:.0%})"
    )
    return MarketState(regime, cap, breadth, note)


# ---------------------------------------------------------------- sector layer

def sector_ranking(ind: Ind, t: pd.Timestamp) -> pd.DataFrame:
    """Sector ETFs ranked by 63d relative strength vs SPY (where money flows)."""
    rows = []
    for etf in SECTORS:
        if etf not in ind.close or pd.isna(ind.rs63_vs_spy.at[t, etf]):
            continue
        rows.append(
            {
                "etf": etf,
                "rs63": ind.rs63_vs_spy.at[t, etf],
                "ret21": ind.ret21.at[t, etf],
                "above50": bool(ind.close.at[t, etf] > ind.sma50.at[t, etf]),
            }
        )
    df = pd.DataFrame(rows).sort_values("rs63", ascending=False).reset_index(drop=True)
    df["rank"] = df.index + 1
    df["leading"] = (df["rank"] <= 4) & (df["rs63"] > 0) & df["above50"]
    return df
