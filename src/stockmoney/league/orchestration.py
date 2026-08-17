"""The league's daily heartbeat: run every active trader over every watchlist
symbol, grade what has matured, and stamp one shared regime per symbol/day so
per-regime league comparisons are apples-to-apples.

Look-ahead / one-ruler discipline: for each symbol a single MarketContext is
built (see context.py) and passed to every engine, so all traders share the
same trade_date, entry_price, grade_vol and regime. Predictions are idempotent
per (trader, symbol, trade_date), so this is safe to re-run nightly.

Decision Points (issue #7 P1, CONTEXT.md): the day is split into scheduled
checkpoints -- `run_predictions(decision_point=...)` for 盤前/pre_market and
事件觸發/event (new opinions allowed), `confirm_and_book` for 開盤後/post_open
(confirm or withdraw the same day's pre_market/event calls, then book),
`grade_matured` + review for 收盤後/post_close (grade + review only, no new
calls). `run_decision_point` is the single top-level entry point that
sequences the right one; the pre-existing functions stay independently
callable (nightly_refresh, the CLI, backfill scripts all call them directly
with `decision_point=None`, the legacy/immediate mode that predates P1).
"""
from __future__ import annotations

from datetime import date, timedelta

import duckdb

from stockmoney.data import trader_predictions as tp
from stockmoney.data import trader_prediction_grades as tpg
from stockmoney.data.ingestion.live_quotes import fetch_live_quotes
from stockmoney.data.positions import price_on_date
from stockmoney.data.traders import list_active_traders
from stockmoney.data.watchlist import sector_for_symbol
from stockmoney.league import ledger
from stockmoney.league import review as review_mod
from stockmoney.league.context import build_context
from stockmoney.league.daily_digest import DigestInputs, build_daily_digest
from stockmoney.league.engines import ENGINE_REGISTRY
from stockmoney.league.grading_options import grade_option_pnl
from stockmoney.league.option_bridge import build_option_structure
from stockmoney.models import production

GRADE_LOOKUP_GRACE_DAYS = 5  # mirror daily_prediction_cli: search forward if label_end lands on a holiday
NEW_CALL_DECISION_POINTS = ("pre_market", "event")
# Macro event types that justify an extra 'event' decision point regardless
# of symbol (story 8: CPI/FOMC/NFP move the whole watchlist, not one ticker).
_MACRO_EVENT_TYPES = ("fomc", "cpi", "nfp")


def _active_watchlist_symbols(conn: duckdb.DuckDBPyConnection) -> list[str]:
    rows = conn.execute(
        "SELECT symbol FROM watchlist_members WHERE removed_date IS NULL ORDER BY symbol"
    ).fetchall()
    return [r[0] for r in rows]


def is_event_day(conn: duckdb.DuckDBPyConnection, as_of: date) -> bool:
    """Story 8 (CONTEXT.md's Decision Point): true if `as_of` has a scheduled
    macro event (CPI/FOMC/NFP -- moves the whole watchlist) or an earnings
    release for an active watchlist symbol, per `event_calendar` (DB-first,
    migration 013). A scheduler (OpenClaw cron, CLAUDE.md section 13) checks
    this before deciding whether to also call
    `run_decision_point(decision_point='event')` on top of the normal
    pre_market/post_open/post_close cadence -- actual OS-level cron wiring is
    external to this module, same as every other batch step here."""
    row = conn.execute(
        """
        SELECT 1 FROM event_calendar
        WHERE CAST(scheduled_at AS DATE) = ?
          AND (
            (symbol IS NULL AND event_type IN ({}))
            OR (event_type = 'earnings' AND symbol IN (
                SELECT symbol FROM watchlist_members WHERE removed_date IS NULL
            ))
          )
        LIMIT 1
        """.format(",".join("?" * len(_MACRO_EVENT_TYPES))),
        [as_of, *_MACRO_EVENT_TYPES],
    ).fetchone()
    return row is not None


def run_predictions(
    conn: duckdb.DuckDBPyConnection, *, horizon: int = production.DEFAULT_HORIZON,
    decision_point: str | None = None,
) -> dict:
    """For every active trader x every active watchlist symbol: build the
    shared context once per symbol, run each trader's engine, record the call.
    Returns a per-trader summary of recorded/skipped, plus skip reasons.

    `decision_point` (issue #7 P1): None is the legacy/immediate mode (every
    existing caller before P1) -- a call is recorded AND its paper-trading
    fill booked in this same pass, exactly like before. 'pre_market'/'event'
    defer booking: the call (and its mechanically-selected option_structure,
    still built here so nothing is ever re-selected with hindsight later) is
    recorded, multi-horizon grade rows are pre-created, but the ledger fill
    only happens once `confirm_and_book` confirms it at the post_open
    decision point -- new opinions never get filled before they've survived
    that confirm/withdraw check."""
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
                thesis=call.thesis,
                evidence_chain=call.evidence_chain,
                rejected_alternatives=call.rejected_alternatives,
                confidence_rationale=call.confidence_rationale,
                decision_point=decision_point,
            )
            summary[trader.trader_id]["recorded"] += 1
            tpg.record_pending_grades(conn, prediction_id=prediction_id, trade_date=ctx.trade_date)

            # Legacy/immediate mode books the fill in this same pass (Wave D
            # behaviour, unchanged). A decision-point-gated call defers
            # booking to confirm_and_book at the post_open checkpoint -- a
            # new opinion is never filled before it has survived that
            # confirm/withdraw check.
            if decision_point is None and call.option_structure is not None:
                try:
                    ledger.open_trade(conn, tp.get_trader_prediction(conn, prediction_id))
                except Exception as exc:
                    skips.append(f"{symbol}/{trader.trader_id}: ledger open_trade failed ({exc})")

    summary["skips"] = skips
    return summary


def confirm_and_book(
    conn: duckdb.DuckDBPyConnection, *, as_of: date | None = None,
    fetch_quotes=fetch_live_quotes,
) -> dict:
    """The post_open decision point (issue #7 P1): for every 'pre_market'/
    'event' call made on `as_of` and not yet confirmed or withdrawn, check
    whether the live intraday quote has already moved the underlying past
    this call's OWN grading band against its direction -- if so the thesis
    was invalidated before it could ever be filled, so withdraw it (avoids
    追高殺低, CONTEXT.md); otherwise confirm it and book the paper-trading
    fill, using the SAME option_structure chosen at pre_market (never
    re-selected with hindsight). A symbol with no live quote available
    (yfinance hiccup) confirms by default -- honest: we can't check, so we
    don't silently block a call that was never actually contradicted."""
    as_of = as_of or date.today()
    pending = tp.predictions_pending_confirmation(conn, as_of)
    symbols = sorted({p.symbol for p in pending})
    quotes = fetch_quotes(symbols) if symbols else {}

    summary = {"confirmed": 0, "withdrawn": 0, "booked": 0, "notes": []}
    for p in pending:
        quote = quotes.get(p.symbol)
        if quote is not None:
            band = tp.band_for(p)
            move = quote["price"] / p.entry_price - 1.0
            adverse = (p.direction == "up" and move < -band) or (p.direction == "down" and move > band)
            if adverse:
                reason = (
                    f"live price already moved {move:+.1%} against '{p.direction}' "
                    f"(band +-{band:.1%}) before the post_open fill"
                )
                tp.withdraw_call(conn, p.prediction_id, reason=reason)
                summary["withdrawn"] += 1
                summary["notes"].append(f"{p.symbol}/{p.trader_id}: withdrawn ({reason})")
                continue

        tp.confirm_call(conn, p.prediction_id)
        summary["confirmed"] += 1
        if p.option_structure is not None:
            try:
                trade_id = ledger.open_trade(conn, tp.get_trader_prediction(conn, p.prediction_id))
                if trade_id is not None:
                    summary["booked"] += 1
            except Exception as exc:
                summary["notes"].append(f"{p.symbol}/{p.trader_id}: ledger open_trade failed ({exc})")

    return summary


def _price_with_grace(conn: duckdb.DuckDBPyConnection, symbol: str, label_end_date: date) -> float | None:
    """Price on `label_end_date`, searching forward a few days if it lands on
    a holiday (mirrors daily_prediction_cli's grade). Shared by both the
    original single-horizon grade and the multi-horizon grade rows below --
    one lookup convention, not two."""
    actual_price = price_on_date(conn, symbol, label_end_date)
    if actual_price is not None:
        return actual_price
    for offset in range(1, GRADE_LOOKUP_GRACE_DAYS + 1):
        actual_price = price_on_date(conn, symbol, label_end_date + timedelta(days=offset))
        if actual_price is not None:
            return actual_price
    return None


def grade_matured(conn: duckdb.DuckDBPyConnection, *, as_of: date | None = None) -> dict:
    """Grade every matured, ungraded trader prediction using the price on its
    label_end_date (with the same holiday grace window as daily_prediction_cli),
    AND independently grade every matured trader_prediction_grades row (issue
    #7 P1's multi-horizon layer, CONTEXT.md's Grading Horizon) -- the same
    call can be 'right at 1 day, wrong at 21 days' and both get recorded.
    Deliberately manual-triggered (like the existing daily_prediction_cli grade),
    not wired into nightly."""
    as_of = as_of or date.today()
    graded, still_pending = 0, 0
    for p in tp.list_pending_trader_predictions(conn, as_of=as_of):
        actual_price = _price_with_grace(conn, p.symbol, p.label_end_date)
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

    horizon_graded = {h: 0 for h in tpg.GRADING_HORIZONS}
    horizon_still_pending = {h: 0 for h in tpg.GRADING_HORIZONS}
    for g in tpg.list_pending_grades(conn, as_of=as_of):
        symbol = conn.execute(
            "SELECT symbol FROM trader_predictions WHERE prediction_id = ?", [g.prediction_id]
        ).fetchone()[0]
        actual_price = _price_with_grace(conn, symbol, g.label_end_date)
        if actual_price is None:
            horizon_still_pending[g.horizon_days] += 1
            continue
        tpg.grade_horizon(conn, prediction_id=g.prediction_id, horizon_days=g.horizon_days, actual_price=actual_price)
        horizon_graded[g.horizon_days] += 1

    return {
        "graded": graded, "still_pending": still_pending,
        "horizon_graded": horizon_graded, "horizon_still_pending": horizon_still_pending,
    }


def run_decision_point(
    conn: duckdb.DuckDBPyConnection, *, decision_point: str, as_of: date | None = None,
    horizon: int = production.DEFAULT_HORIZON,
) -> dict:
    """Top-level entry point (issue #7 P1): drives the right step(s) for one
    of the day's scheduled checkpoints. The individual steps
    (run_predictions/confirm_and_book/grade_matured/review) stay
    independently callable -- this only sequences which of them belong to
    which decision point:
      - 'pre_market' / 'event': new calls may be issued -> run_predictions.
      - 'post_open': confirm or withdraw today's pre_market/event calls, and
        book the survivors -> confirm_and_book.
      - 'post_close': no new calls; grade what matured (multi-horizon) and
        review, then produce the plain-language daily digest.
    Foreman selection / challenger promotion (P2/P3) are not built yet -- the
    post_close sequence here is grading -> review -> digest, matching what
    this phase actually has to report."""
    as_of = as_of or date.today()
    if decision_point in NEW_CALL_DECISION_POINTS:
        return run_predictions(conn, horizon=horizon, decision_point=decision_point)
    if decision_point == "post_open":
        return confirm_and_book(conn, as_of=as_of)
    if decision_point == "post_close":
        grading = grade_matured(conn, as_of=as_of)
        review_summary = review_mod.run_review(conn)
        recorded, confirmed, withdrawn = tp.todays_call_counts(conn, as_of)
        digest_text = build_daily_digest(DigestInputs(
            as_of=as_of,
            market_note=f"{grading['graded']} 筆判斷到期評分,{review_summary['reviewed']} 筆完成覆盤。",
            predictions_recorded=recorded, confirmed=confirmed, withdrawn=withdrawn,
            graded=grading["graded"], still_pending=grading["still_pending"],
            decisions_needed=(
                [f"{review_summary['method_proposals']} 筆方法改進提案待人工審核"]
                if review_summary["method_proposals"] else []
            ),
        ))
        return {"grading": grading, "review": review_summary, "digest": digest_text}
    raise ValueError(f"unknown decision_point: {decision_point!r}")
