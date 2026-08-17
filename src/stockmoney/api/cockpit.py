"""Honest Lin-cockpit backend (see CLAUDE.md / REBUILD_PLAN.md Phase 0-1c
"time-machine" scoreboard verdict): six leak-safe backtests found NO
systematic 1-3 day directional edge in price signals, real GDELT news, or
high-vol momentum. The only durable findings across 8y of walk-forward
testing are (1) being long (beta) beats every directional strategy tried and
(2) selling small OTM puts has a tiny but statistically significant positive
edge (a variance-risk-premium harvest, not a directional call) that shrinks
close to zero once a naive price-based risk gate is layered on.

So this module does NOT predict direction. It computes plain descriptive
state -- price levels, breakout/pullback state, realized-vol regime, and a
sell-put income idea gated the same way the backtest gated it (Phase 1c:
close < SMA50 or realized vol elevated vs its own 6-month history => sit
out) -- for a human discretionary trader to read alongside the news feed.
Every number here is read-only, computed from ohlcv_daily/news_items at
request time (a handful of symbols, so no caching layer is needed the way
/api/quotes needs one for the live yfinance overlay).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import duckdb

from stockmoney.api import queries
from stockmoney.data.news_synthesis import _is_genuine_macro
from stockmoney.models.strike_ladder import snap_strike

# Same OTM% used by the sell-put strategies in scripts/premium_selling_backtest.py
# and stockmoney.backtest.highvol_strategies.SELLPUT_OTM -- kept identical here
# so the "suggested strike" on the cockpit matches what was actually backtested,
# not a made-up new number.
SELLPUT_OTM = 0.05

# Lookback windows for the descriptive levels. N-day high/low uses a
# standard "20 trading days" (~1 month) window; SMA20/50 are the two most
# common trend references and are what the sell-put risk gate itself uses.
NDAY_WINDOW = 20
SMA_SHORT = 20
SMA_LONG = 50

# Realized-vol regime gate: compare today's 20d realized vol to its own
# trailing 6-month (~126 trading day) distribution. This mirrors the exact
# gate tested in REBUILD_PLAN.md section 4.12 ("Phase 1c-gated"): sit out
# when close < SMA50 OR realized vol is in its own elevated (80th pct+)
# regime. That experiment found the gate cuts tail losses roughly a third but
# also erases the strategy's (already tiny) statistical significance -- so
# this gate is surfaced as a descriptive flag for a human to weigh, not
# proof the gated version has edge.
RV_LOOKBACK_DAYS = 126
RV_ELEVATED_PCTL = 0.80

# --- v2 additions (2026-07-14): still descriptive-only, still never a
# direction call. Volume, sector-linkage and sector-rotation all answer
# "what is happening right now / how does it compare to the name's own
# history or its peers" -- not "which way does it go next". See
# WORKER6_AUTONOMOUS_SPEC.md.

# Relative-volume window: today's volume vs. its own trailing 20-trading-day
# average (today excluded from the average so a big print never inflates its
# own baseline). 20 days matches the SMA20/NDAY_WINDOW convention already
# used above.
VOLUME_AVG_WINDOW = 20
VOLUME_HIGH_RATIO = 1.5
VOLUME_LOW_RATIO = 0.6

# "Does this name trade with its sector today, or on its own?" -- a plain-
# language surfacing of CLAUDE.md section 8's cross-sectional dispersion idea
# (there it's a regime-detection *feature*; here it's a per-symbol descriptive
# read). z is the symbol's today return minus its sector PEERS' mean return
# (self excluded), divided by the peers' return stdev. |z| >= 1 is an
# arbitrary-but-documented "notably away from the pack" cutoff -- not a
# calibrated threshold, since there's no backtest behind this v2 addition.
SECTOR_DISPERSION_Z_THRESHOLD = 1.0

# Sector-rotation ranking window for the 5-day column.
SECTOR_ROTATION_5D_WINDOW = 5

# Human labels for the watchlist's sector codes (see watchlist_members.sector).
# ETF sector buckets are excluded from rotation/linkage ranking (see
# sector_rotation/sector_linkage_map docstrings) but kept here for any display
# that still wants a readable name for them.
SECTOR_CN: dict[str, str] = {
    "semiconductor": "半導體",
    "semiconductor_etf": "半導體 ETF",
    "big_tech": "大型科技",
    "big_tech_etf": "大型科技 ETF",
    "energy": "能源",
    "financials": "金融",
}

# How far back to look for real-world macro/event news for the morning
# narrative (Fed / oil / geopolitics / earnings-week items -- item_type =
# 'macro', symbol IS NULL). 72h rather than 24h because this data pipeline is
# a daily batch job, not intraday -- a Friday afternoon run still needs to see
# Thursday's Fed headline.
NARRATIVE_MACRO_LOOKBACK_HOURS = 72
NARRATIVE_MACRO_LIMIT = 4

STABLE_FINDINGS = {
    "as_of": "2026-07-13",
    "headline": "8 年 walk-forward 回測:純價格/技術訊號沒有一個打贏單純做多;唯一顯著正 edge 是賣方 OTM put 的極小 variance risk premium。",
    "conclusions": [
        {
            "id": "no_directional_edge",
            "text": "現行方向模型、順勢動能、橫向選股三條路徑,在 1-3 天期權持有期下都沒有統計顯著的方向性優勢(部分甚至顯著虧損)。",
        },
        {
            "id": "long_bias_wins",
            "text": "裸多(always-long call)在樣本期間打贏所有測試過的方向性策略 —— 這多半是半導體 8 年牛市紅利,不是可複製的「選股能力」。",
        },
        {
            "id": "sellput_small_edge",
            "text": "賣 5% OTM put(5-10 個交易日到期)是唯一 95% 信賴區間顯著淨正的策略,但單筆報酬極小(+0.05~0.11%)、尾端虧損兇(-46%~-63%),且遠遜於單純做多的報酬。",
        },
        {
            "id": "gate_kills_significance",
            "text": "對賣方策略加上價格風控閘門(close<SMA50 或 realized vol 高分位)可把尾端虧損縮小約三成,但報酬也跟著歸零、不再顯著 —— 這點正報酬的來源本身就是「恐慌時賣」。",
        },
        {
            "id": "news_untested",
            "text": "真實新聞(非 GDELT)僅約 1.5 年歷史,樣本不足以嚴謹回測;GDELT 11 年歷史但先前已測方向性不顯著,尚未以新聞當『賣方風控閘門』重新驗證。",
        },
    ],
    "so_what": "本工具不預測漲跌。它呈現現價、關鍵價位、regime、新聞,以及依實測閘門邏輯算出的『今天適不適合賣 put 收租』燈號 —— 判斷仍由人做。",
}


@dataclass
class SymbolLevels:
    close: float | None
    nday_high: float | None
    nday_low: float | None
    prev_high: float | None
    prev_low: float | None
    sma20: float | None
    sma50: float | None


def _price_series(conn: duckdb.DuckDBPyConnection, symbol: str, *, limit_days: int = 300) -> list[tuple]:
    """Ascending (trade_date, open, high, low, close, volume) rows, most recent
    `limit_days` -- enough for SMA50 + a 6-month realized-vol history with
    margin. Deduped to one row per trade_date the same way ticker_detail does
    (latest ingested_at wins) so a same-day re-ingest never double-counts."""
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT trade_date, open, high, low, close, volume,
                   row_number() OVER (PARTITION BY trade_date ORDER BY ingested_at DESC) AS rn
            FROM ohlcv_daily WHERE symbol = ? AND close IS NOT NULL
        )
        SELECT trade_date, open, high, low, close, volume FROM latest WHERE rn = 1
        ORDER BY trade_date DESC LIMIT ?
        """,
        [symbol, limit_days],
    ).fetchall()
    return list(reversed(rows))  # ascending


def _sma(closes: list[float], n: int) -> float | None:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def compute_levels(rows: list[tuple]) -> SymbolLevels:
    if not rows:
        return SymbolLevels(None, None, None, None, None, None, None)
    closes = [r[4] for r in rows]
    highs = [r[2] for r in rows]
    lows = [r[3] for r in rows]
    close = closes[-1]
    window_highs = highs[-NDAY_WINDOW:]
    window_lows = lows[-NDAY_WINDOW:]
    prev_high = highs[-2] if len(highs) >= 2 else None
    prev_low = lows[-2] if len(lows) >= 2 else None
    return SymbolLevels(
        close=close,
        nday_high=max(window_highs) if window_highs else None,
        nday_low=min(window_lows) if window_lows else None,
        prev_high=prev_high,
        prev_low=prev_low,
        sma20=_sma(closes, SMA_SHORT),
        sma50=_sma(closes, SMA_LONG),
    )


def volume_signal(rows: list[tuple]) -> dict:
    """v2 item 3: today's volume vs. its own trailing 20-day average --
    plain relative-volume (RVOL) read. High volume means more participation
    / conviction behind today's move (in either direction); it says nothing
    about which way price goes next, only how much trading activity is
    behind today's print. `rows` is _price_series's (date, o, h, l, c, v)
    tuples; degrades to "資料不足" when there isn't a full 20-day-plus-today
    volume history yet (new listing, gap in ingestion, etc.)."""
    volumes = [r[5] for r in rows if r[5] is not None]
    if len(volumes) < VOLUME_AVG_WINDOW + 1:
        return {"ratio": None, "state": "資料不足", "today_volume": None, "avg_volume_20d": None}

    today = volumes[-1]
    baseline = volumes[-(VOLUME_AVG_WINDOW + 1):-1]
    avg = sum(baseline) / len(baseline)
    if avg <= 0:
        return {"ratio": None, "state": "資料不足", "today_volume": today, "avg_volume_20d": None}

    ratio = today / avg
    if ratio >= VOLUME_HIGH_RATIO:
        state = "放量"
    elif ratio <= VOLUME_LOW_RATIO:
        state = "縮量"
    else:
        state = "量能正常"
    return {
        "ratio": ratio,
        "state": state,
        "today_volume": today,
        "avg_volume_20d": avg,
    }


def breakout_state(levels: SymbolLevels) -> str:
    """Descriptive state only -- NOT a directional prediction. Just "where is
    price relative to its own recent range and prior day", the same reference
    frame a discretionary trader like Lin annotates on a chart by hand."""
    if levels.close is None or levels.nday_high is None or levels.nday_low is None:
        return "資料不足"
    if levels.close >= levels.nday_high:
        return "創新高" if levels.nday_high == levels.close else "接近區間高點"
    if levels.close <= levels.nday_low:
        return "創新低" if levels.nday_low == levels.close else "接近區間低點"
    if levels.prev_high is not None and levels.close > levels.prev_high:
        return "站上前日高點"
    if levels.prev_low is not None and levels.close < levels.prev_low:
        return "跌破前日低點"
    return "區間內盤整"


# Machine-readable grouping key for breakout_state's display string, so a
# frontend that buckets cards into "偏強/中性/偏弱" sections reads a stable
# key instead of re-matching Chinese text (a wording change here used to
# silently reclassify a card on the frontend, with no build error and no test
# failure -- see Opportunities.tsx's POSTURE_OF_STATE, now removed in favour
# of this being the one place the mapping lives).
BREAKOUT_POSTURE: dict[str, str] = {
    "創新高": "strong",
    "接近區間高點": "strong",
    "站上前日高點": "strong",
    "區間內盤整": "neutral",
    "創新低": "weak",
    "接近區間低點": "weak",
    "跌破前日低點": "weak",
    "資料不足": "neutral",
}


def posture_of(state: str) -> str:
    return BREAKOUT_POSTURE.get(state, "neutral")


def realized_vol_series(rows: list[tuple], *, window: int = SMA_SHORT) -> list[float]:
    """Annualized rolling realized vol from close-to-close log-ish returns
    (simple pct returns, std * sqrt(252) -- consistent with realized_vol_20d
    used across the rest of the codebase, e.g. models.regime's observation
    vector). One value per day once >= window+1 closes are available."""
    closes = [r[4] for r in rows]
    if len(closes) < window + 1:
        return []
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes)) if closes[i - 1]]
    out = []
    for i in range(window, len(rets) + 1):
        chunk = rets[i - window:i]
        mean = sum(chunk) / len(chunk)
        var = sum((x - mean) ** 2 for x in chunk) / (len(chunk) - 1) if len(chunk) > 1 else 0.0
        out.append((var ** 0.5) * (252 ** 0.5))
    return out


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, max(0, round(p * (len(s) - 1))))
    return s[idx]


def sellput_gate(levels: SymbolLevels, rv_series: list[float]) -> dict:
    """Reproduces REBUILD_PLAN.md section 4.12's exact gate: sit out when
    close < SMA50, OR today's realized vol sits at/above the 80th percentile
    of its own trailing ~6-month history. That backtest found the gate
    shrinks tail losses but also erases the (already marginal) statistical
    significance of the underlying sell-put edge -- so a green light here
    means "the naive price gate doesn't flag this name today", not "this
    trade has proven positive expectancy."""
    reasons = []
    if levels.close is None or levels.sma50 is None:
        return {"gate_light": "unknown", "reason": "資料不足,無法計算 SMA50 閘門", "rv_now": None, "rv_80pctl_6m": None}

    below_sma50 = levels.close < levels.sma50
    if below_sma50:
        reasons.append(f"收盤 {levels.close:.2f} 低於 SMA50 {levels.sma50:.2f}")

    rv_now = rv_series[-1] if rv_series else None
    rv_hist = rv_series[-RV_LOOKBACK_DAYS:] if rv_series else []
    rv_80 = _percentile(rv_hist, RV_ELEVATED_PCTL) if rv_hist else None
    elevated_rv = rv_now is not None and rv_80 is not None and rv_now >= rv_80
    if elevated_rv:
        reasons.append(f"已實現波動率 {rv_now:.1%} 處於近 6 個月前 20% 高分位(門檻 {rv_80:.1%})")

    if below_sma50 or elevated_rv:
        return {
            "gate_light": "red",
            "reason": "；".join(reasons) + "。閘門建議空手,不賣 put(規則來自回測 Phase 1c-gated)。",
            "rv_now": rv_now,
            "rv_80pctl_6m": rv_80,
        }
    return {
        "gate_light": "green",
        "reason": "收盤在 SMA50 之上,且波動率未處於自身 6 個月高分位 —— 閘門不擋單,可考慮收租(非勝率保證)。",
        "rv_now": rv_now,
        "rv_80pctl_6m": rv_80,
    }


def sellput_suggestion(levels: SymbolLevels, gate: dict) -> dict | None:
    if levels.close is None:
        return None
    # Snap to a real listed strike so the suggested contract is one a broker
    # would actually show (data-integrity audit 2026-07-15).
    strike = snap_strike(levels.close * (1 - SELLPUT_OTM))
    return {
        "strike": strike,
        "otm_pct": SELLPUT_OTM,
        "gate_light": gate["gate_light"],
        "reason": gate["reason"],
        "note": "示意履約價(現價 -5% OTM),對應回測用的窗口;非即時報價,實際下單前務必用自己的選擇權鏈核對。",
    }


def _watchlist_returns(conn: duckdb.DuckDBPyConnection) -> dict[str, dict]:
    """1-day and trailing-5-trading-day pct return per watchlist symbol, one
    query for the whole list. Reused by sector_rotation, sector_linkage_map
    and build_narrative so "today's return" means the exact same number
    (same trade_date, same dedup rule) in all three places -- no risk of the
    sector-strength ranking and a symbol's "does it match its sector" read
    quietly disagreeing about what "today" is."""
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT symbol, trade_date, close,
                   row_number() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS rn
            FROM ohlcv_daily WHERE close IS NOT NULL
        )
        SELECT symbol, rn, trade_date, close FROM latest WHERE rn <= ?
        """,
        [SECTOR_ROTATION_5D_WINDOW + 1],
    ).fetchall()
    by_symbol: dict[str, dict[int, tuple]] = {}
    for symbol, rn, trade_date, close in rows:
        by_symbol.setdefault(symbol, {})[rn] = (trade_date, close)

    out: dict[str, dict] = {}
    for symbol, ranks in by_symbol.items():
        d0, c0 = ranks.get(1, (None, None))
        _, c1 = ranks.get(2, (None, None))
        _, c5 = ranks.get(SECTOR_ROTATION_5D_WINDOW + 1, (None, None))
        ret_1d = (c0 - c1) / c1 if c0 is not None and c1 else None
        ret_5d = (c0 - c5) / c5 if c0 is not None and c5 else None
        out[symbol] = {"as_of_date": d0, "ret_1d": ret_1d, "ret_5d": ret_5d}
    return out


def sector_linkage_map(members: list[dict], returns: dict[str, dict]) -> dict[str, dict]:
    """v2 item 4: per-symbol "is it moving with its sector today, or on its
    own?" -- a plain-language surfacing of CLAUDE.md section 8's
    idiosyncratic/systematic dispersion idea. z-score of the symbol's today
    return against its sector PEERS' mean return (self excluded, so a symbol
    can't just be near its own value), divided by the peers' return stdev.
    Descriptive only: a large |z| means today's move is concentrated in this
    one name rather than shared across the sector (a hint there's
    name-specific news to go read), NOT a signal of which way it goes next.

    ETF sector buckets (semiconductor_etf, big_tech_etf) are marked "不適用"
    rather than compared against "peers": an ETF IS the sector's basket (or a
    leveraged/inverse derivative of it, e.g. SOXL/SOXS), not one stock among
    peers inside it -- "does this ETF move with its own sector" isn't a
    meaningful idiosyncratic-news question the way it is for a single name.
    """
    by_sector: dict[str, list[str]] = {}
    for m in members:
        by_sector.setdefault(m["sector"], []).append(m["symbol"])

    out: dict[str, dict] = {}
    for sector, symbols in by_sector.items():
        if sector.endswith("_etf"):
            for s in symbols:
                out[s] = {
                    "state": "不適用",
                    "symbol_return": returns.get(s, {}).get("ret_1d"),
                    "peer_avg_return": None,
                    "z": None,
                    "note": "ETF 本身就是追蹤一籃子個股的基金(部分為槓桿/反向倍數產品),它就是板塊的代表,不是板塊裡的一個個股,不適用「跟不跟板塊」離散度比較。",
                }
            continue

        vals = {s: returns[s]["ret_1d"] for s in symbols if returns.get(s, {}).get("ret_1d") is not None}
        for s in symbols:
            peer_rets = [v for peer, v in vals.items() if peer != s]
            if s not in vals or len(peer_rets) < 2:
                out[s] = {
                    "state": "資料不足", "symbol_return": vals.get(s),
                    "peer_avg_return": None, "z": None, "note": None,
                }
                continue
            mean = sum(peer_rets) / len(peer_rets)
            var = sum((v - mean) ** 2 for v in peer_rets) / len(peer_rets)
            std = var ** 0.5
            r = vals[s]
            if std <= 1e-9:
                out[s] = {
                    "state": "跟隨板塊同步", "symbol_return": r,
                    "peer_avg_return": mean, "z": 0.0, "note": None,
                }
                continue
            z = (r - mean) / std
            state = "脫離板塊獨走" if abs(z) >= SECTOR_DISPERSION_Z_THRESHOLD else "跟隨板塊同步"
            out[s] = {"state": state, "symbol_return": r, "peer_avg_return": mean, "z": z, "note": None}
    return out


def sector_rotation(members: list[dict], returns: dict[str, dict]) -> list[dict]:
    """v2 item 5: which sectors are strongest/weakest right now -- average
    today's return and average trailing-5-day return across each sector's
    members, ranked. A breadth/rotation READ ("where is money moving today"),
    not a forecast of which sector moves next.

    ETF sector buckets are excluded (same reasoning as sector_linkage_map):
    ranking "semiconductor_etf" alongside "semiconductor" would double-count
    the same underlying names, and a 3x-leveraged/inverse product's averaged-
    in return would distort the sector's own organic strength/weakness read.
    """
    by_sector: dict[str, list[str]] = {}
    for m in members:
        if m["sector"].endswith("_etf"):
            continue
        by_sector.setdefault(m["sector"], []).append(m["symbol"])

    out: list[dict] = []
    for sector, symbols in by_sector.items():
        d1 = [returns[s]["ret_1d"] for s in symbols if returns.get(s, {}).get("ret_1d") is not None]
        d5 = [returns[s]["ret_5d"] for s in symbols if returns.get(s, {}).get("ret_5d") is not None]
        if not d1:
            continue
        out.append({
            "sector": sector,
            "sector_label": SECTOR_CN.get(sector, sector),
            "n_symbols": len(symbols),
            "ret_1d_avg": sum(d1) / len(d1),
            "ret_5d_avg": (sum(d5) / len(d5)) if d5 else None,
        })
    out.sort(key=lambda r: r["ret_1d_avg"], reverse=True)
    for i, r in enumerate(out):
        r["rank"] = i + 1
    return out


import statistics as _stats

# The free self-estimated IV surface (yfinance) is NOISY -- especially very
# short-dated buckets, which throw absurd values (CVX's front expiry reads 162%
# while its next reads 3%; SOXL has a 2043% row). Surfacing the nearest-expiry
# number as-is showed "IV 162.5%" on a large-cap card, which reads as a bug.
# So take a ROBUST estimate: the median of 50-delta IVs across near-dated
# (<=120d) future expiries, keeping only values in a plausible [5%, 120%] band.
# A symbol with no plausible value is absent -> None ("IV 資料不足"). This is
# still only a rough free estimate (a real IV feed is needed to trust it -- see
# the options-microstructure memory), never a forecast.
IV_PLAUSIBLE_LO = 0.05
IV_PLAUSIBLE_HI = 1.20


def _iv_by_symbol(conn: duckdb.DuckDBPyConnection) -> dict[str, float | None]:
    """Robust ATM (50-delta) implied vol per symbol (see module note above):
    median of plausible near-dated 50-delta IVs at each symbol's latest
    iv_surface_daily trade_date; None if none are plausible. A descriptive
    fraction (e.g. 0.286 = 28.6%), never fabricated, never a forecast."""
    rows = conn.execute(
        """
        WITH latest_trade AS (
            SELECT symbol, max(trade_date) AS trade_date
            FROM iv_surface_daily
            GROUP BY symbol
        )
        SELECT i.symbol, i.implied_vol
        FROM iv_surface_daily i
        JOIN latest_trade lt
          ON i.symbol = lt.symbol AND i.trade_date = lt.trade_date
        WHERE i.delta_bucket = '50'
          AND i.expiry_date >= i.trade_date
          AND i.expiry_date <= i.trade_date + INTERVAL 120 DAY
        """
    ).fetchall()
    by_sym: dict[str, list[float]] = {}
    for sym, iv in rows:
        if iv is not None and IV_PLAUSIBLE_LO <= iv <= IV_PLAUSIBLE_HI:
            by_sym.setdefault(sym, []).append(iv)
    return {s: _stats.median(v) for s, v in by_sym.items() if v}


def _earnings_dates_from_calendar(
    conn: duckdb.DuckDBPyConnection, symbols: list[str]
) -> dict[str, date]:
    """DB-first source for next earnings date: `event_calendar` (migration
    013). Empty in the live DB as of this writing, so this returns {} today
    -- kept as the first-choice source so a future ingester (or a manual
    entry) is picked up automatically with no code change here.
    `earnings_calendar.py`'s Nasdaq network scan is only the fallback for
    whatever this query doesn't cover."""
    if not symbols:
        return {}
    placeholders = ",".join(["?"] * len(symbols))
    rows = conn.execute(
        f"""
        WITH latest AS (
            SELECT symbol, scheduled_at,
                   row_number() OVER (PARTITION BY event_id ORDER BY ingested_at DESC) AS rn
            FROM event_calendar
            WHERE event_type = 'earnings' AND symbol IN ({placeholders})
              AND scheduled_at >= current_date
        )
        SELECT symbol, min(scheduled_at) FROM latest WHERE rn = 1 GROUP BY symbol
        """,
        symbols,
    ).fetchall()
    return {r[0]: (r[1].date() if hasattr(r[1], "date") else r[1]) for r in rows}


def _earnings_date_map(
    conn: duckdb.DuckDBPyConnection, symbols: list[str]
) -> dict[str, date | None]:
    """Best-effort next-earnings-date per symbol (CLAUDE.md Task D part 2):
    `event_calendar` first (DB, currently empty), Nasdaq's free earnings-
    calendar API (earnings_calendar.py) as the fallback for whatever
    event_calendar doesn't have. Never fabricates a date -- a symbol neither
    source finds maps to None, which the frontend renders as "財報日未知"."""
    from stockmoney.data.earnings_calendar import get_next_earnings_dates

    out: dict[str, date | None] = dict(_earnings_dates_from_calendar(conn, symbols))
    missing = [s for s in symbols if s not in out]
    if missing:
        try:
            out.update(get_next_earnings_dates(missing))
        except Exception:
            # Network fallback must never break the cockpit endpoint --
            # degrade to "unknown" for whatever it couldn't resolve.
            for s in missing:
                out[s] = None
    return out


def cockpit_symbol(
    conn: duckdb.DuckDBPyConnection,
    symbol: str,
    *,
    regime_label: str,
    top_news: dict | None,
    sector_link: dict | None = None,
    iv: float | None = None,
    earnings_date: date | None = None,
) -> dict:
    rows = _price_series(conn, symbol)
    levels = compute_levels(rows)
    state = breakout_state(levels)
    rv_series = realized_vol_series(rows)
    gate = sellput_gate(levels, rv_series)
    sellput = sellput_suggestion(levels, gate)
    volume = volume_signal(rows)
    as_of = rows[-1][0] if rows else None
    # v2 item (2026-07-14): dollar volume = today's close x today's volume,
    # both already loaded by _price_series -- a plain liquidity/size fact
    # ("how much dollar value traded today"), not a signal.
    today_volume = rows[-1][5] if rows else None
    dollar_volume = (
        levels.close * today_volume if levels.close is not None and today_volume is not None else None
    )
    return {
        "symbol": symbol,
        "as_of_date": as_of,
        "price": levels.close,
        "levels": {
            "nday_high": levels.nday_high,
            "nday_low": levels.nday_low,
            "prev_high": levels.prev_high,
            "prev_low": levels.prev_low,
            "sma20": levels.sma20,
            "sma50": levels.sma50,
        },
        "breakout_state": state,
        "posture": posture_of(state),
        "regime": regime_label,
        "top_news": top_news,
        "sellput": sellput,
        "volume_signal": volume,
        "sector_linkage": sector_link or {
            "state": "資料不足", "symbol_return": None, "peer_avg_return": None, "z": None, "note": None,
        },
        # v2 items (2026-07-14, Task D part 2): descriptive facts, never a
        # direction call -- see _iv_by_symbol / _earnings_date_map docstrings.
        "iv": iv,
        "dollar_volume": dollar_volume,
        "earnings_date": earnings_date,
    }


def build_cockpit(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    """Per-symbol decision cards for the whole core watchlist. Reuses
    queries.py's already-computed regime label map and latest-news lookup
    (same freshness/ranking rules as the rest of the app) so the cockpit
    never re-derives a second, possibly-inconsistent notion of "today's
    regime" or "today's headline" for a symbol."""
    members = queries.watchlist_core(conn)
    label_map = queries.regime_label_map(conn)
    regimes = queries.latest_regime_by_symbol(conn)
    symbol_news = queries.latest_symbol_news(conn)
    returns = _watchlist_returns(conn)
    linkage_map = sector_linkage_map(members, returns)
    iv_map = _iv_by_symbol(conn)
    earnings_map = _earnings_date_map(conn, [m["symbol"] for m in members])
    out = []
    for m in members:
        symbol = m["symbol"]
        regime_id = regimes.get(symbol)
        label = queries.regime_label(regime_id, label_map)
        card = cockpit_symbol(
            conn, symbol, regime_label=label, top_news=symbol_news.get(symbol),
            sector_link=linkage_map.get(symbol),
            iv=iv_map.get(symbol), earnings_date=earnings_map.get(symbol),
        )
        card["sector"] = m["sector"]
        out.append(card)
    return out


def _level_line(card: dict) -> str:
    """One Lin-style morning-note line per symbol: current price, the level
    that matters today, and what each side of it means -- descriptive, never
    a directional call. Falls back to a plain 'no data' line rather than
    guessing when history is too short for a level."""
    symbol = card["symbol"]
    price = card["price"]
    levels = card["levels"]
    if price is None:
        return f"{symbol}:資料不足,今日無法標註關鍵價位。"

    parts = [f"{symbol} 現價 {price:.2f}"]
    high, low = levels.get("nday_high"), levels.get("nday_low")
    if high is not None and low is not None:
        parts.append(
            f"今日觀察 {NDAY_WINDOW} 日區間 {low:.2f}~{high:.2f}:突破 {high:.2f} 站穩則區間偏強,跌破 {low:.2f} 則區間偏弱"
        )
    sma50 = levels.get("sma50")
    if sma50 is not None:
        rel = "站上" if price >= sma50 else "跌破"
        parts.append(f"目前{rel} SMA50({sma50:.2f})")
    if card.get("sellput") is not None:
        gl = card["sellput"]["gate_light"]
        gl_cn = {"green": "🟢 賣方閘門開", "red": "🔴 賣方閘門關", "unknown": "⚪ 賣方閘門資料不足"}[gl]
        parts.append(gl_cn)
    return "；".join(parts) + "。"


# Root-cause fix (2026-07-14): the coffee-tariff/UAW/student-loan/"cheapest
# states" lifestyle noise that used to slip through as item_type='macro' is
# now filtered upstream, at classification time, by
# stockmoney.data.news_synthesis.classify_type -> _is_genuine_macro (whole-
# word macro-theme match + denylist -- the same allow/deny pass that used to
# live only here). So news_items.item_type='macro' rows reaching this query
# should already be clean by construction. This call is kept as a *cheap,
# redundant-by-design* backstop -- defence in depth against any future
# upstream regression -- not because it's still doing the real work.
def _is_market_relevant_macro(headline: str) -> bool:
    return _is_genuine_macro(headline or "")


def _recent_macro_headlines(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    """Real-world macro/event news for the narrative (v2 item 1): Fed, oil,
    geopolitics, rates -- item_type='macro' rows have symbol IS NULL (market-
    wide, not one ticker's news), ingested the same way as every other
    news_items row (see queries.news_feed). Ranked by importance then
    recency, generic template headlines filtered the same way
    queries.is_generic_headline filters the per-symbol news line, capped to
    NARRATIVE_MACRO_LIMIT so the narrative paragraph stays a paragraph and
    not a list dump."""
    rows = conn.execute(
        """
        SELECT headline, source_name, published_at, importance, sentiment_score
        FROM news_items
        WHERE symbol IS NULL AND item_type = 'macro'
          AND published_at >= now() - (? * INTERVAL 1 HOUR)
        ORDER BY coalesce(importance, 0) DESC, published_at DESC
        """,
        [NARRATIVE_MACRO_LOOKBACK_HOURS],
    ).fetchall()
    items = [
        {
            "headline": r[0], "source_name": r[1], "published_at": r[2],
            "importance": r[3], "sentiment_score": r[4],
        }
        for r in rows
        if not queries.is_generic_headline(r[0]) and _is_market_relevant_macro(r[0])
    ]
    return items[:NARRATIVE_MACRO_LIMIT]


def build_narrative(conn: duckdb.DuckDBPyConnection, market: dict, rotation: list[dict]) -> dict:
    """v2 item 1: the upgraded morning-note paragraph. Synthesizes real
    macro/event news (Fed / oil / geopolitics / earnings, via
    _recent_macro_headlines) with the already-computed regime mix (market)
    and sector-rotation ranking (rotation) into one narrative paragraph --
    describing what IS happening and at what levels/readings, never which way
    anything goes next.

    Per WORKER6_AUTONOMOUS_SPEC.md's default branch: when there isn't a real,
    non-generic macro headline within the lookback window, this falls back to
    a regime + sector-strength-only paragraph rather than inventing macro
    colour that isn't backed by an actual ingested news item -- `basis` tells
    the frontend/report which paragraph shape was used."""
    macro_items = _recent_macro_headlines(conn)

    vix = market["vix"]
    if vix is not None:
        slope_txt = "期限結構偏向恐慌後仰(短天期波動比長天期貴)" if (market["vix_term_slope"] or 0) < 0 else "期限結構平靜正常"
        vix_txt = f"VIX 現為 {vix:.1f},{slope_txt}"
    else:
        vix_txt = "VIX 資料不足"

    regime_txt = f"核心觀察清單 {market['n_symbols']} 檔中,主導市場狀態為「{market['dominant_regime'] or '—'}」"

    sector_txt = ""
    if rotation:
        strongest, weakest = rotation[0], rotation[-1]
        if strongest["sector"] != weakest["sector"]:
            sector_txt = (
                f"板塊輪動上,今日 {strongest['sector_label']} 平均報酬最高"
                f"({strongest['ret_1d_avg']:+.1%}),{weakest['sector_label']} 相對最弱"
                f"({weakest['ret_1d_avg']:+.1%})。"
            )
        else:
            sector_txt = f"今日僅 {strongest['sector_label']} 板塊有足夠資料可比較,平均報酬 {strongest['ret_1d_avg']:+.1%}。"

    if macro_items:
        clauses = [f"「{it['headline']}」({it['source_name'] or '來源未標示'})" for it in macro_items]
        macro_txt = "近期總經/事件面消息包括:" + "；".join(clauses) + "。"
        text = (
            f"{regime_txt},{vix_txt}。{macro_txt}{sector_txt}"
            "以上為市場現況與消息事實描述,不構成漲跌預測,進出場判斷仍需自行評估。"
        )
        basis = "macro_news"
    else:
        text = (
            f"{regime_txt},{vix_txt}。{sector_txt}"
            "近期(72小時內)無足夠總經/事件新聞可綜合,以上退回 regime + 板塊強弱簡版摘要;不構成漲跌預測。"
        )
        basis = "fallback_regime_sector"

    return {"text": text, "basis": basis, "macro_items": macro_items}


def build_briefing(conn: duckdb.DuckDBPyConnection) -> dict:
    """The daily narrative object: overall market regime/breadth (from
    market_summary's already-computed breadth/VIX/regime mix) plus a
    per-symbol 'today watch level X; if it breaks -> ..., if it holds -> ...'
    line built from the same levels/news/gate the cockpit cards show.
    Deliberately narrative text over structured numbers a human already sees
    elsewhere on the page -- this is the "read once at market open" summary,
    not a new data source."""
    market = queries.market_summary(conn)
    members = queries.watchlist_core(conn)
    returns = _watchlist_returns(conn)
    rotation = sector_rotation(members, returns)
    narrative = build_narrative(conn, market, rotation)
    cards = build_cockpit(conn)

    dc = market["direction_counts"]
    total = sum(dc.values()) or 1
    breadth_line = (
        f"核心觀察清單 {market['n_symbols']} 檔中,主導 regime 為「{market['dominant_regime'] or '—'}」"
        f"(VIX {market['vix']:.1f}" + (f",期限結構{'恐慌後仰' if (market['vix_term_slope'] or 0) < 0 else '平靜正常'}" if market["vix"] is not None else "") + ")。"
        if market["vix"] is not None
        else f"核心觀察清單 {market['n_symbols']} 檔中,主導 regime 為「{market['dominant_regime'] or '—'}」。"
    )

    lines = [_level_line(c) for c in cards]

    # v2 item (homepage stats strip): plain counts of watchlist names that
    # closed up/down/flat today, reusing the exact same `returns` used for
    # sector_rotation above -- so this is a descriptive breadth READ ("how
    # many names moved which way today"), not a re-derivation of the old
    # per-symbol "direction" prediction column build_narrative deliberately
    # avoids. Together with VIX + term-structure mood (already computed in
    # `market`) this backs the compact market-stats strip on the homepage,
    # freeing up the space the old, taller sector-rotation panel used.
    breadth = {"up": 0, "down": 0, "flat": 0}
    for r in returns.values():
        ret = r.get("ret_1d")
        if ret is None:
            continue
        if ret > 0:
            breadth["up"] += 1
        elif ret < 0:
            breadth["down"] += 1
        else:
            breadth["flat"] += 1

    return {
        "as_of_date": market["as_of_date"],
        "headline": breadth_line,
        # v2 item 1: the fuller morning-note paragraph (macro news + regime +
        # sector strength). `headline` above is kept as the short one-line
        # breadth summary for backward compatibility; `narrative` is the new
        # richer text the frontend now leads with.
        "narrative": narrative["text"],
        "narrative_basis": narrative["basis"],
        # v2 item 5: sector-rotation ranking (today's + trailing-5-day avg
        # return per sector), strongest first.
        "sector_rotation": rotation,
        # Compact market-stats strip inputs: VIX level + term-structure mood
        # (same fields build_narrative already reads off `market`) plus the
        # breadth count above.
        "vix": market["vix"],
        "vix_term_slope": market["vix_term_slope"],
        "breadth": breadth,
        "guardrail": (
            "無方向 edge;賣方收租是唯一微弱正 edge;做多贏過一切 —— "
            "本工具輔助你的判斷,不預測方向。"
        ),
        "market_lines": [breadth_line],
        "symbol_lines": lines,
        "scoreboard": STABLE_FINDINGS,
    }


def scoreboard_summary() -> dict:
    """Fixed, human-reviewed findings text (REBUILD_PLAN.md Phase 0-1c) --
    deliberately NOT a live re-run of scripts/scoreboard_cli.py's
    run_scoreboard (that walk-forward takes ~2 minutes; a request thread
    should never block on it). If the underlying experiments are re-run and
    the conclusions change, this dict is the one place to update by hand."""
    return STABLE_FINDINGS
