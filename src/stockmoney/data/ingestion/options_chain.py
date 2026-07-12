from __future__ import annotations

import math
from datetime import date, datetime, timezone

import duckdb
import polars as pl

from stockmoney.data.ingestion.base import run_multi_ingestion
from stockmoney.data.options_math import bs_delta, is_valid_option, naive_gex

# v1 simplifications (CLAUDE.md section 16 "待資料驗證的參數"):
DEFAULT_RISK_FREE_RATE = 0.045   # flat r; could later be wired to FRED DGS2
DEFAULT_MIN_DTE = 7
DEFAULT_MAX_DTE = 270            # core direction cycle is 1-9 months
DEFAULT_SKEW_TARGET_DTE = 30     # representative tenor for the 25-delta skew metric
METHOD_VERSION = "v1_oi_iv_bs"

_IV_SCHEMA = {
    "symbol": pl.Utf8,
    "trade_date": pl.Date,
    "expiry_date": pl.Date,
    "delta_bucket": pl.Utf8,
    "implied_vol": pl.Float64,
    "source": pl.Utf8,
}
_PCR_SCHEMA = {
    "symbol": pl.Utf8,
    "trade_date": pl.Date,
    "put_volume": pl.Int64,
    "call_volume": pl.Int64,
    "put_oi": pl.Int64,
    "call_oi": pl.Int64,
    "source": pl.Utf8,
}
_DERIVED_SCHEMA = {
    "symbol": pl.Utf8,
    "trade_date": pl.Date,
    "metric_name": pl.Utf8,
    "metric_value": pl.Float64,
    "method_version": pl.Utf8,
    "source": pl.Utf8,
}


def _safe_int(x) -> int:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return 0
    return int(x)


def _closest_bucket(rows: list[tuple[float, float]], target_abs_delta: float):
    """From ``(abs_delta, iv)`` pairs pick the IV whose delta is nearest the
    target. Returns None if there are no valid rows."""
    if not rows:
        return None
    best = min(rows, key=lambda pair: abs(pair[0] - target_abs_delta))
    return best[1]


def _process_symbol(
    symbol: str,
    spot: float,
    expiries: list[str],
    chain_getter,
    trade_date: date,
    r: float,
    min_dte: int,
    max_dte: int,
    skew_target_dte: int,
):
    iv_rows: list[dict] = []
    put_vol = call_vol = put_oi = call_oi = 0
    gex_strikes: list[tuple[float, float, bool]] = []
    gex_t: list[float] = []
    gex_sigma: list[float] = []
    skew_by_dte: dict[int, dict[str, float]] = {}  # dte -> {'25c': iv, '25p': iv}

    for exp_str in expiries:
        expiry_date = date.fromisoformat(exp_str)
        dte = (expiry_date - trade_date).days
        if dte < min_dte or dte > max_dte:
            continue
        t = dte / 365.0

        chain = chain_getter(exp_str)
        for frame, is_call, bucket_atm, bucket_wing in (
            (chain.calls, True, "50", "25c"),
            (chain.puts, False, None, "25p"),
        ):
            strikes = frame["strike"].tolist()
            ivs = frame["impliedVolatility"].tolist()
            ois = frame["openInterest"].tolist()
            vols = frame["volume"].tolist()

            call_deltas: list[tuple[float, float]] = []  # (delta, iv) for 50-bucket
            wing_deltas: list[tuple[float, float]] = []   # (abs_delta, iv) for 25-bucket

            for strike, sigma, oi, vol in zip(strikes, ivs, ois, vols):
                oi_i, vol_i = _safe_int(oi), _safe_int(vol)
                if is_call:
                    call_vol += vol_i
                    call_oi += oi_i
                else:
                    put_vol += vol_i
                    put_oi += oi_i

                if not is_valid_option(spot, strike, t, sigma):
                    continue
                delta = bs_delta(spot, strike, t, sigma, r, is_call=is_call)
                gex_strikes.append((strike, oi_i, is_call))
                gex_t.append(t)
                gex_sigma.append(sigma)

                if is_call:
                    call_deltas.append((delta, sigma))
                wing_deltas.append((abs(delta), sigma))

            if bucket_atm is not None:
                atm_iv = _closest_bucket(
                    [(abs(d - 0.50), iv) for d, iv in call_deltas], 0.0
                )
                if atm_iv is not None:
                    iv_rows.append(_iv_row(symbol, trade_date, expiry_date, "50", atm_iv))

            wing_iv = _closest_bucket(wing_deltas, 0.25)
            if wing_iv is not None:
                iv_rows.append(_iv_row(symbol, trade_date, expiry_date, bucket_wing, wing_iv))
                skew_by_dte.setdefault(dte, {})[bucket_wing] = wing_iv

    derived_rows = _derived_rows(
        symbol, trade_date, spot, gex_strikes, gex_t, gex_sigma, r,
        skew_by_dte, skew_target_dte,
    )
    pcr_row = {
        "symbol": symbol, "trade_date": trade_date,
        "put_volume": put_vol, "call_volume": call_vol,
        "put_oi": put_oi, "call_oi": call_oi, "source": "yfinance",
    }
    return iv_rows, pcr_row, derived_rows


def _iv_row(symbol, trade_date, expiry_date, bucket, iv) -> dict:
    return {
        "symbol": symbol, "trade_date": trade_date, "expiry_date": expiry_date,
        "delta_bucket": bucket, "implied_vol": float(iv), "source": "yfinance",
    }


def _derived_rows(
    symbol, trade_date, spot, gex_strikes, gex_t, gex_sigma, r,
    skew_by_dte, skew_target_dte,
) -> list[dict]:
    rows = []
    if gex_strikes:
        gex = naive_gex(gex_strikes, spot, gex_t, gex_sigma, r)
        rows.append(_derived_row(symbol, trade_date, "gex_estimate", gex))

    # 25-delta skew at the tenor closest to the target DTE, if both wings exist.
    usable = {
        dte: legs for dte, legs in skew_by_dte.items()
        if "25c" in legs and "25p" in legs
    }
    if usable:
        nearest = min(usable, key=lambda dte: abs(dte - skew_target_dte))
        legs = usable[nearest]
        rows.append(
            _derived_row(symbol, trade_date, "skew_25delta", legs["25p"] - legs["25c"])
        )
    return rows


def _derived_row(symbol, trade_date, metric_name, metric_value) -> dict:
    return {
        "symbol": symbol, "trade_date": trade_date, "metric_name": metric_name,
        "metric_value": float(metric_value), "method_version": METHOD_VERSION,
        "source": "yfinance",
    }


def fetch_options_snapshot(
    symbols: list[str],
    *,
    trade_date: date | None = None,
    r: float = DEFAULT_RISK_FREE_RATE,
    min_dte: int = DEFAULT_MIN_DTE,
    max_dte: int = DEFAULT_MAX_DTE,
    skew_target_dte: int = DEFAULT_SKEW_TARGET_DTE,
) -> dict[str, pl.DataFrame]:
    """Pull the current options chain for ``symbols`` and derive the IV
    surface (25/50-delta buckets), put/call ratios, and GEX/skew estimates.

    yfinance option chains are a *snapshot only* (no history), so these
    features can only accumulate forward from the day this first runs — they
    cannot be backfilled. ``trade_date`` therefore defaults to today.
    """
    import yfinance as yf

    trade_date = trade_date or datetime.now(timezone.utc).date()
    iv_rows: list[dict] = []
    pcr_rows: list[dict] = []
    derived_rows: list[dict] = []
    errors: list[Exception] = []

    for symbol in symbols:
        try:
            ticker = yf.Ticker(symbol)
            expiries = list(ticker.options)
            if not expiries:
                continue
            spot = _resolve_spot(ticker)
            if spot is None:
                continue
            s_iv, s_pcr, s_derived = _process_symbol(
                symbol, spot, expiries, ticker.option_chain,
                trade_date, r, min_dte, max_dte, skew_target_dte,
            )
            iv_rows.extend(s_iv)
            pcr_rows.append(s_pcr)
            derived_rows.extend(s_derived)
        except Exception as exc:  # yfinance is flaky; tolerate per-symbol failures
            errors.append(exc)

    if errors and not iv_rows and not pcr_rows:
        raise errors[0]

    return {
        "iv_surface_daily": pl.DataFrame(iv_rows, schema=_IV_SCHEMA) if iv_rows else pl.DataFrame(schema=_IV_SCHEMA),
        "put_call_ratio_daily": pl.DataFrame(pcr_rows, schema=_PCR_SCHEMA) if pcr_rows else pl.DataFrame(schema=_PCR_SCHEMA),
        "options_derived_daily": pl.DataFrame(derived_rows, schema=_DERIVED_SCHEMA) if derived_rows else pl.DataFrame(schema=_DERIVED_SCHEMA),
    }


def _resolve_spot(ticker) -> float | None:
    try:
        px = ticker.fast_info.get("lastPrice")
        if px:
            return float(px)
    except Exception:
        pass
    try:
        hist = ticker.history(period="1d")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception:
        pass
    return None


def _latest_trading_day(conn: duckdb.DuckDBPyConnection) -> date | None:
    row = conn.execute("SELECT max(trade_date) FROM ohlcv_daily").fetchone()
    return row[0] if row else None


def ingest_watchlist_options(
    conn: duckdb.DuckDBPyConnection, *, trade_date: date | None = None
) -> dict[str, int]:
    """Ingest an options-chain snapshot for every active watchlist symbol into
    iv_surface_daily, put_call_ratio_daily and options_derived_daily.

    ``trade_date`` defaults to the latest real trading day already recorded in
    ``ohlcv_daily`` -- NOT wall-clock "today". yfinance always serves *some*
    chain (spot/IV reflecting the last close) even when called on a weekend or
    holiday, so blindly stamping `date.today()` would silently mint a snapshot
    dated a day no other table ever has a row for. Since this data is
    snapshot-only and can never be backfilled (see fetch_options_snapshot's
    docstring), an orphaned trade_date isn't just a cosmetic label -- it's a
    permanently lost day of the one options signal that's actually free.
    """
    trade_date = trade_date or _latest_trading_day(conn) or datetime.now(timezone.utc).date()
    symbols = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT symbol FROM watchlist_members "
            "WHERE removed_date IS NULL ORDER BY symbol"
        ).fetchall()
    ]
    results = run_multi_ingestion(
        conn,
        source="yfinance_options",
        window_start=trade_date,
        window_end=trade_date,
        fetch_fn=lambda: fetch_options_snapshot(symbols, trade_date=trade_date),
    )
    return {table: res.rows_written for table, res in results.items()}
