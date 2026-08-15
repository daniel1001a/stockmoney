"""The league's daily heartbeat: run every active trader over every watchlist
symbol, grade what has matured, and stamp one shared regime per symbol/day so
per-regime league comparisons are apples-to-apples.

Look-ahead / one-ruler discipline: for each symbol a single MarketContext is
built (see context.py) and passed to every engine, so all traders share the
same trade_date, entry_price, grade_vol and regime. Predictions are idempotent
per (trader, symbol, trade_date), so this is safe to re-run nightly.
"""
from __future__ import annotations

from datetime import date, timedelta

import duckdb

from stockmoney.data import trader_predictions as tp
from stockmoney.data.positions import price_on_date
from stockmoney.data.traders import list_active_traders
from stockmoney.data.watchlist import sector_for_symbol
from stockmoney.league import ledger
from stockmoney.league.context import build_context
from stockmoney.league.engines import ENGINE_REGISTRY
from stockmoney.league.grading_options import grade_option_pnl
from stockmoney.league.option_bridge import build_option_structure
from stockmoney.models import production

GRADE_LOOKUP_GRACE_DAYS = 5  # mirror daily_prediction_cli: search forward if label_end lands on a holiday


def _active_watchlist_symbols(conn: duckdb.DuckDBPyConnection) -> list[str]:
    rows = conn.execute(
        "SELECT symbol FROM watchlist_members WHERE removed_date IS NULL ORDER BY symbol"
    ).fetchall()
    return [r[0] for r in rows]


def run_predictions(
    conn: duckdb.DuckDBPyConnection, *, horizon: int = production.DEFAULT_HORIZON
) -> dict:
    """For every active trader x every active watchlist symbol: build the
    shared context once per symbol, run each trader's engine, record the call.
    Returns a per-trader summary of recorded/skipped, plus skip reasons."""
    traders = list_active_traders(conn)
    engines = [(t, ENGINE_REGISTRY.get(t.engine_key)) for t in traders]

    summary: dict = {t.trader_id: {"recorded": 0, "skipped": 0} for t in traders}
    skips: list[str] = []

    for symbol in _active_watchlist_symbols(conn):
        sector = sector_for_symbol(conn, symbol)
        if sector is None:
            skips.append(f"{symbol}: not resolvable to a feature-group sector")
            for t in traders:
                summary[t.trader_id]["skipped"] += 1
            continue

        ctx, ctx_skip = build_context(conn, symbol=symbol, sector=sector, horizon=horizon)
        if ctx is None:
            skips.append(f"{symbol}: {ctx_skip}")
            for t in traders:
                summary[t.trader_id]["skipped"] += 1
            continue

        for trader, engine in engines:
            if engine is None:
                summary[trader.trader_id]["skipped"] += 1
                skips.append(f"{symbol}/{trader.trader_id}: no engine for key {trader.engine_key!r}")
                continue
            call, skip_reason = engine.predict(conn, ctx)
            if call is None:
                summary[trader.trader_id]["skipped"] += 1
                skips.append(f"{symbol}/{trader.trader_id}: {skip_reason}")
                continue

            # Instrument selection is mechanical, applied identically to every
            # trader's call -- see league/option_bridge.py's module docstring
            # for why this lives here rather than inside each engine.
            #
            # build_option_structure/select_option already return None
            # gracefully for the routine "no usable entry IV" cases (see
            # their own guards); this try/except is the last line of defense
            # for anything those guards don't anticipate (e.g. strike_ladder's
            # NaN/positivity guard firing on a strike that slipped through --
            # that guard is intentionally strict and must stay). One bad
            # symbol must never abort predictions for every other symbol in
            # the league (2026-07-15 incident: an uncaught ValueError here
            # took down the whole nightly run_predictions call) -- the
            # directional call itself is still perfectly valid and gets
            # recorded, just without an instrument attached.
            try:
                call.option_structure = build_option_structure(conn, ctx, call)
            except Exception as exc:
                call.option_structure = None
                skips.append(f"{symbol}/{trader.trader_id}: option structure build failed ({exc}), recording call without an instrument")

            prediction_id = tp.record_trader_prediction(
                conn,
                trader_id=trader.trader_id,
                method_version=call.method_version,
                trade_date=ctx.trade_date,
                symbol=ctx.symbol,
                sector=ctx.sector,
                horizon=ctx.horizon,
                label_end_date=ctx.label_end_date,
                direction=call.direction,
                conviction=call.conviction,
                rationale=call.rationale,
                invalidation=call.invalidation,
                entry_price=ctx.entry_price,
                grade_vol=ctx.grade_vol,
                engine_payload=call.engine_payload,
                regime=ctx.regime,          # shared regime -> per-regime comparability
                band_k=ctx.band_k,
                available_at=ctx.available_at,
                option_structure=call.option_structure,
            )
            summary[trader.trader_id]["recorded"] += 1

            # Book the paper-trading fill for this call (Wave D). Same
            # last-line-of-defense reasoning as the option-structure build
            # above: one trader's booking failure must never abort the whole
            # league predict pass.
            if call.option_structure is not None:
                try:
                    ledger.open_trade(conn, tp.get_trader_prediction(conn, prediction_id))
                except Exception as exc:
                    skips.append(f"{symbol}/{trader.trader_id}: ledger open_trade failed ({exc})")

    summary["skips"] = skips
    return summary


def grade_matured(conn: duckdb.DuckDBPyConnection, *, as_of: date | None = None) -> dict:
    """Grade every matured, ungraded trader prediction using the price on its
    label_end_date (with the same holiday grace window as daily_prediction_cli).
    Deliberately manual-triggered (like the existing daily_prediction_cli grade),
    not wired into nightly."""
    as_of = as_of or date.today()
    graded, still_pending = 0, 0
    for p in tp.list_pending_trader_predictions(conn, as_of=as_of):
        actual_price = price_on_date(conn, p.symbol, p.label_end_date)
        if actual_price is None:
            for offset in range(1, GRADE_LOOKUP_GRACE_DAYS + 1):
                actual_price = price_on_date(conn, p.symbol, p.label_end_date + timedelta(days=offset))
                if actual_price is not None:
                    break
        if actual_price is None:
            still_pending += 1
            continue
        tp.grade_trader_prediction(conn, p.prediction_id, actual_price=actual_price)
        # Real option P&L, on the same actual_price the directional grade
        # just used -- days_held uses the nominal (trade_date, label_end_date)
        # span, matching backtest_options_pnl.py's convention even when the
        # grace-window search above found the price a few days later.
        days_held = (p.label_end_date - p.trade_date).days
        grade_option_pnl(conn, p.prediction_id, exit_spot=actual_price, days_held=days_held)
        # Close the paper-trading position this call opened (Wave D), reusing
        # the exact same actual_price -- no-op if nothing was ever booked for
        # it (no instrument, or insufficient cash at entry time).
        ledger.close_trade(conn, tp.get_trader_prediction(conn, p.prediction_id))
        graded += 1
    return {"graded": graded, "still_pending": still_pending}
