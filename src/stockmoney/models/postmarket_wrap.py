"""Post-market (end-of-day) wrap-up generator.

Companion to `stockmoney.api.cockpit.build_briefing` (the pre-market note):
where that module reads "what should I watch today", this one reads "what
just happened today and why (if we honestly know)". Same honesty discipline
as the rest of the app (CLAUDE.md section 0 / cockpit.py's module docstring):

  - No direction prediction. This never says which way anything goes next --
    only what already happened (closing moves, breadth, regime, VIX) and,
    where a real ingested news item exists in the right time window, that a
    headline co-occurred with the move. Co-occurrence is flagged as
    UNVERIFIED correlation, never asserted as causation.
  - When no matching news item exists for a mover, that is stated explicitly
    ("查無明確相關消息") rather than inventing a plausible-sounding catalyst.
    Fabricating a cause for a move is exactly what CLAUDE.md forbids.
  - No look-ahead: every query below is bounded to `<= as_of_date` (prices)
    or a news window ending at the end of `as_of_date` (headlines), so a
    historical replay of this function never sees data that wasn't yet
    available on the day being summarized. This matters if `build_postmarket_wrap`
    is ever run over past dates for backtesting the wrap generator itself,
    not just live "run tonight" usage.

Layering note: this module lives under `stockmoney.models` (per the
assignment that created it), but reads from `stockmoney.api.queries` /
`stockmoney.api.cockpit` -- the reverse of this codebase's usual
models -> api dependency direction. That's deliberate here: it reuses
already-tested logic (regime labels, sector rotation, generic-headline
filtering) instead of re-deriving a second, possibly-inconsistent copy of it.
If this direction is undesirable long-term, the shared pieces
(`sector_rotation`, `is_generic_headline`, `regime_label[_map]`) could move to
a lower-level shared module -- flagged for the foreman, not fixed here since
this task's file ownership is scoped to this one new file.

Every function takes an already-open DuckDB connection (read-only by
convention, same as api/queries.py) and never fits/trains anything live.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import duckdb

from stockmoney.api import cockpit, queries

# A move at/above this magnitude is "notable" enough to call out explicitly
# in the `notable` one-liners. Arbitrary-but-documented, like cockpit.py's
# SECTOR_DISPERSION_Z_THRESHOLD -- not calibrated against any backtest, just
# a reasonable screen so quiet ±0.3% days don't fill the list with noise.
NOTABLE_MOVE_THRESHOLD = 0.02
NOTABLE_MAX_ITEMS = 8

# How many gainers / losers to surface in `top_movers` (each side, before
# de-duplication on a very small watchlist).
MOVERS_TOP_N = 5

# News lookback window for "what may have driven today's move" -- deliberately
# short (same-day-ish) rather than the 10-day window api/queries.py's
# opportunity board uses: a wrap-up explaining TODAY's close should not credit
# a headline from four days ago as today's driver.
DRIVER_NEWS_MAX_AGE_DAYS = 2


def _resolve_as_of_date(conn: duckdb.DuckDBPyConnection, symbols: list[str]) -> date | None:
    """Latest trade_date with a close among the given symbols. None if the
    watchlist has no price history at all yet (honest empty state)."""
    if not symbols:
        return None
    placeholders = ",".join(["?"] * len(symbols))
    row = conn.execute(
        f"SELECT max(trade_date) FROM ohlcv_daily WHERE close IS NOT NULL AND symbol IN ({placeholders})",
        symbols,
    ).fetchone()
    return row[0] if row else None


def _moves_asof(
    conn: duckdb.DuckDBPyConnection, symbols: list[str], as_of_date: date
) -> dict[str, dict]:
    """Per-symbol {as_of_date, close, prev_close, change_pct, ret_1d, ret_5d}
    using only trade_date <= as_of_date (no look-ahead). `ret_1d`/`ret_5d`
    keys are included (ret_5d always None -- this wrap only needs 1-day
    moves) purely so this dict can be fed straight into
    `cockpit.sector_rotation`, which expects that shape; avoids re-deriving
    sector-average logic here."""
    if not symbols:
        return {}
    placeholders = ",".join(["?"] * len(symbols))
    rows = conn.execute(
        f"""
        WITH latest AS (
            SELECT symbol, trade_date, close,
                   row_number() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS rn
            FROM ohlcv_daily
            WHERE close IS NOT NULL AND trade_date <= ? AND symbol IN ({placeholders})
        )
        SELECT symbol, rn, trade_date, close FROM latest WHERE rn <= 2
        """,
        [as_of_date, *symbols],
    ).fetchall()
    by_symbol: dict[str, dict[int, tuple]] = {}
    for symbol, rn, trade_date, close in rows:
        by_symbol.setdefault(symbol, {})[rn] = (trade_date, close)

    out: dict[str, dict] = {}
    for symbol, ranks in by_symbol.items():
        d0, c0 = ranks.get(1, (None, None))
        _, c1 = ranks.get(2, (None, None))
        if c0 is None or not c1:
            continue
        change_pct = (c0 - c1) / c1
        out[symbol] = {
            "as_of_date": d0, "close": c0, "prev_close": c1,
            "change_pct": change_pct, "ret_1d": change_pct, "ret_5d": None,
        }
    return out


def _regime_counts_asof(
    conn: duckdb.DuckDBPyConnection, symbols: list[str], as_of_date: date, label_map: dict[int, str]
) -> dict[str, int]:
    """How many watchlist symbols are (as of as_of_date, no look-ahead) in
    each labeled regime -- the wrap's "dominant regime" is the mode of this."""
    if not symbols:
        return {}
    placeholders = ",".join(["?"] * len(symbols))
    try:
        rows = conn.execute(
            f"""
            WITH latest AS (
                SELECT symbol, regime,
                       row_number() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS rn
                FROM daily_predictions
                WHERE trade_date <= ? AND symbol IN ({placeholders})
            )
            SELECT symbol, regime FROM latest WHERE rn = 1
            """,
            [as_of_date, *symbols],
        ).fetchall()
    except duckdb.Error:
        return {}
    counts: dict[str, int] = {}
    for _symbol, regime in rows:
        label = queries.regime_label(regime, label_map)
        counts[label] = counts.get(label, 0) + 1
    return counts


def _vix_asof(conn: duckdb.DuckDBPyConnection, as_of_date: date) -> tuple[float | None, float | None]:
    """(vix_spot, vix_term_slope) as of the latest vix_term_structure_daily
    row on or before as_of_date -- same 30d spot / (90d-9d) slope convention
    as api.queries.market_summary, just cutoff-bounded here."""
    try:
        rows = conn.execute(
            """
            WITH latest AS (SELECT max(trade_date) AS d FROM vix_term_structure_daily WHERE trade_date <= ?)
            SELECT tenor_days, vix_value FROM vix_term_structure_daily
            WHERE trade_date = (SELECT d FROM latest)
            """,
            [as_of_date],
        ).fetchall()
    except duckdb.Error:
        return None, None
    by_tenor = {t: v for t, v in rows}
    spot = by_tenor.get(30)
    slope = (
        by_tenor.get(90) - by_tenor.get(9)
        if by_tenor.get(90) is not None and by_tenor.get(9) is not None else None
    )
    return spot, slope


def _driver_headline_asof(
    conn: duckdb.DuckDBPyConnection, symbol: str, as_of_date: date,
    *, max_age_days: int = DRIVER_NEWS_MAX_AGE_DAYS,
) -> dict | None:
    """The single most-important real news item for `symbol` published in the
    `max_age_days` window ending at the end of as_of_date (UTC) -- i.e. only
    news that existed by the close being summarized, never later news
    (no look-ahead). None if nothing genuine is found; generic
    template headlines (queries.is_generic_headline) are excluded the same
    way the rest of the app excludes them, so a "Stock Quote" boilerplate
    never masquerades as today's driver."""
    cutoff_end = datetime.combine(as_of_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=1)
    cutoff_start = cutoff_end - timedelta(days=max_age_days)
    try:
        rows = conn.execute(
            """
            SELECT headline, source_name, published_at, sentiment_score, importance
            FROM news_items
            WHERE symbol = ? AND published_at >= ? AND published_at < ?
            ORDER BY coalesce(importance, 0) DESC, published_at DESC
            """,
            [symbol, cutoff_start, cutoff_end],
        ).fetchall()
    except duckdb.Error:
        return None
    for headline, source_name, published_at, sentiment_score, importance in rows:
        if queries.is_generic_headline(headline):
            continue
        return {
            "headline": headline, "source_name": source_name, "published_at": published_at,
            "sentiment_score": sentiment_score, "importance": importance,
        }
    return None


def _format_pct(x: float) -> str:
    return f"{x:+.1%}"


def _top_movers(
    conn: duckdb.DuckDBPyConnection, moves: dict[str, dict], sector_by_symbol: dict[str, str | None],
    as_of_date: date,
) -> list[dict]:
    """Top gainers + top losers (each up to MOVERS_TOP_N, de-duplicated for a
    small watchlist), each with a same-day driver headline if one genuinely
    exists (never fabricated -- see _driver_headline_asof)."""
    ranked = sorted(moves.items(), key=lambda kv: kv[1]["change_pct"], reverse=True)
    picked: list[str] = [s for s, _ in ranked[:MOVERS_TOP_N]]
    for symbol, _ in ranked[-MOVERS_TOP_N:]:
        if symbol not in picked:
            picked.append(symbol)

    out = []
    for symbol in picked:
        m = moves[symbol]
        driver = _driver_headline_asof(conn, symbol, as_of_date)
        out.append({
            "symbol": symbol,
            "sector": sector_by_symbol.get(symbol),
            "change_pct": m["change_pct"],
            "close": m["close"],
            "driver_headline": driver["headline"] if driver else None,
            "driver_source": driver["source_name"] if driver else None,
            "driver_published_at": driver["published_at"] if driver else None,
            "driver_sentiment": driver["sentiment_score"] if driver else None,
        })
    out.sort(key=lambda r: r["change_pct"], reverse=True)
    return out


def _build_notable(top_movers: list[dict]) -> list[str]:
    """Short honest one-liners for the notable-moves section. A move with a
    genuine same-day news hit gets an explicit "unverified correlation" caveat
    (co-occurrence is not proof of causation); a move with NO matching news is
    labeled exactly that -- "no known driver" -- rather than guessing one, per
    CLAUDE.md's ban on fabricating a catalyst."""
    lines = []
    for m in top_movers:
        if abs(m["change_pct"]) < NOTABLE_MOVE_THRESHOLD:
            continue
        pct = _format_pct(m["change_pct"])
        if m["driver_headline"]:
            source = m["driver_source"] or "來源未標示"
            lines.append(
                f"{m['symbol']} {pct} —— 同期有相關消息:『{m['driver_headline']}』({source});"
                "因果關聯未經統計驗證,僅供參考。"
            )
        else:
            lines.append(f"{m['symbol']} {pct} 但查無明確相關消息 —— 可能為板塊/大盤 beta,也可能是本系統尚未涵蓋的消息來源。")
    return lines[:NOTABLE_MAX_ITEMS]


def _build_headline(sector_strength: list[dict] | None, moves: dict[str, dict]) -> str:
    """One honest sentence summarizing the day -- sector-level if at least two
    sectors have comparable data (e.g. "科技股領漲,半導體走弱"), else falls
    back to the single best/worst mover. Never invents a "why", only "what"."""
    if sector_strength and len(sector_strength) >= 2 and sector_strength[0]["sector"] != sector_strength[-1]["sector"]:
        strongest, weakest = sector_strength[0], sector_strength[-1]
        return (
            f"{strongest['sector_label']}領漲({_format_pct(strongest['ret_1d_avg'])}),"
            f"{weakest['sector_label']}相對疲弱({_format_pct(weakest['ret_1d_avg'])})。"
        )
    if moves:
        ranked = sorted(moves.items(), key=lambda kv: kv[1]["change_pct"], reverse=True)
        top_symbol, top = ranked[0]
        bot_symbol, bot = ranked[-1]
        if top_symbol != bot_symbol:
            return f"{top_symbol} 領漲 {_format_pct(top['change_pct'])},{bot_symbol} 領跌 {_format_pct(bot['change_pct'])}。"
        return f"{top_symbol} {_format_pct(top['change_pct'])}。"
    return "今日收盤資料不足,無法產生總結。"


def _build_narrative(
    as_of_date: date | None, headline: str, breadth: dict, dominant_regime: str | None,
    vix: float | None, vix_term_slope: float | None, notable: list[str],
) -> str:
    date_txt = as_of_date.isoformat() if as_of_date else "—"
    # headline already ends with its own "。" -- strip it here so joining with
    # "，" below doesn't produce a doubled "。，" punctuation glitch.
    headline_clause = headline[:-1] if headline.endswith("。") else headline
    parts = [f"{date_txt} 收盤總結:{headline_clause}"]
    total = breadth["up"] + breadth["down"] + breadth["flat"]
    if total:
        parts.append(f"核心觀察清單中 {breadth['up']} 檔上漲、{breadth['down']} 檔下跌、{breadth['flat']} 檔持平")
    if dominant_regime:
        parts.append(f"主導市場狀態為「{dominant_regime}」")
    if vix is not None:
        slope_txt = "短天期波動較貴(恐慌後仰)" if (vix_term_slope or 0) < 0 else "期限結構平靜"
        parts.append(f"VIX {vix:.1f},{slope_txt}")
    text = "，".join(parts) + "。"
    if notable:
        text += " " + " ".join(notable[:3])
    text += " 以上為收盤事實描述與可得消息的時間關聯提示,因果未經統計驗證,不構成漲跌預測,亦不建議依此下單。"
    return text


def build_postmarket_wrap(conn: duckdb.DuckDBPyConnection, as_of_date: date | None = None) -> dict:
    """Build the after-close daily wrap-up for the core watchlist.

    Parameters
    ----------
    conn: an already-open DuckDB connection (read-only by convention, same as
        api/queries.py and api/cockpit.py).
    as_of_date: the trading day to summarize. None (the normal "run tonight
        after close" case) resolves to the latest date with a close among the
        watchlist symbols. An explicit past date replays the wrap for that
        day using only data available by its close (no look-ahead) -- useful
        for spot-checking this generator against a known day, not just live use.

    Returns a JSON-serializable dict (see module docstring for the honesty
    rules governing every field). Never raises for missing/thin data --
    degrades to an explicit "資料不足" state instead (CLAUDE.md's
    decision-support-only mandate: this must never crash a wrapping FastAPI
    route, and must never silently show a fabricated number).
    """
    members = queries.watchlist_core(conn)
    symbols = [m["symbol"] for m in members]
    sector_by_symbol = {m["symbol"]: m["sector"] for m in members}

    resolved_date = as_of_date or _resolve_as_of_date(conn, symbols)
    if resolved_date is None:
        return {
            "as_of_date": None,
            "headline": "資料不足,尚無收盤價資料可產生今日總結。",
            "narrative": "資料不足,尚無收盤價資料可產生今日總結。",
            "dominant_regime": None,
            "vix": None,
            "vix_term_slope": None,
            "breadth": {"up": 0, "down": 0, "flat": 0},
            "top_movers": [],
            "sector_strength": None,
            "notable": [],
            "data_sufficient": False,
            "insufficient_reason": "watchlist_members 或 ohlcv_daily 尚無資料",
        }

    moves = _moves_asof(conn, symbols, resolved_date)

    breadth = {"up": 0, "down": 0, "flat": 0}
    for m in moves.values():
        if m["change_pct"] > 0:
            breadth["up"] += 1
        elif m["change_pct"] < 0:
            breadth["down"] += 1
        else:
            breadth["flat"] += 1

    if not moves:
        return {
            "as_of_date": resolved_date,
            "headline": "資料不足,今日收盤資料不足以計算個股漲跌。",
            "narrative": "資料不足,今日收盤資料不足以計算個股漲跌(至少需要兩個交易日的收盤價)。",
            "dominant_regime": None,
            "vix": None,
            "vix_term_slope": None,
            "breadth": breadth,
            "top_movers": [],
            "sector_strength": None,
            "notable": [],
            "data_sufficient": False,
            "insufficient_reason": "watchlist symbols 缺少至少兩個交易日的收盤價,無法計算漲跌幅",
        }

    label_map = queries.regime_label_map(conn)
    regime_counts = _regime_counts_asof(conn, symbols, resolved_date, label_map)
    dominant_regime = max(regime_counts.items(), key=lambda kv: kv[1])[0] if regime_counts else None

    vix, vix_term_slope = _vix_asof(conn, resolved_date)

    sector_strength = cockpit.sector_rotation(members, moves) or None

    top_movers = _top_movers(conn, moves, sector_by_symbol, resolved_date)
    notable = _build_notable(top_movers)
    headline = _build_headline(sector_strength, moves)
    narrative = _build_narrative(resolved_date, headline, breadth, dominant_regime, vix, vix_term_slope, notable)

    return {
        "as_of_date": resolved_date,
        "headline": headline,
        "narrative": narrative,
        "dominant_regime": dominant_regime,
        "vix": vix,
        "vix_term_slope": vix_term_slope,
        "breadth": breadth,
        "top_movers": top_movers,
        "sector_strength": sector_strength,
        "notable": notable,
        "data_sufficient": True,
        "insufficient_reason": None,
    }
