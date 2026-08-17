"""Decision Points (issue #7 P1, CONTEXT.md): 盤前/pre_market and 事件觸發/event
issue new calls but defer booking; 開盤後/post_open confirms or withdraws them
and books the survivors; 收盤後/post_close only grades + reviews. Mirrors the
monkeypatch style of test_orchestration.py's existing incident-regression
tests (stub engine + fake context, no real feature data needed)."""
from __future__ import annotations

from datetime import date, datetime, timezone

import duckdb
import numpy as np
import pytest

from stockmoney.data import trader_predictions as tp
from stockmoney.data.db import run_migrations
from stockmoney.league import orchestration
from stockmoney.league.context import MarketContext
from stockmoney.league.engines import EngineCall
from stockmoney.models.production import ProductionPrediction

TRADE_DATE = date(2026, 7, 15)
END = date(2026, 7, 22)


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def _fake_ctx(symbol="NVDA", entry_price=100.0):
    prod = ProductionPrediction(
        as_of_date=TRADE_DATE, symbol=symbol, sector="semiconductor", horizon=5,
        regime=0, proba=np.array([0.1, 0.2, 0.7]),
        feature_values={"realized_vol_20d": 0.4}, model_version="test",
    )
    return MarketContext(
        symbol=symbol, sector="semiconductor", trade_date=TRADE_DATE, horizon=5,
        label_end_date=END, entry_price=entry_price, grade_vol=0.4, band_k=0.5,
        regime=0, available_at=datetime.now(timezone.utc), production=prod,
    )


class _StubEngine:
    key = "stub"

    def predict(self, conn, ctx):
        return (
            EngineCall(
                direction="up", conviction=0.8, rationale="r", invalidation="i",
                method_version="stub-v1", engine_payload={},
            ),
            None,
        )


_STRUCTURE = {"spot": 100.0, "strike": 105.0, "is_call": True, "iv": 0.4,
              "iv_source": "proxy", "t_years": 30 / 365, "dte_days": 30, "side": "long"}


def _wire_stub(monkeypatch, *, entry_price=100.0):
    monkeypatch.setattr(orchestration, "_active_watchlist_symbols", lambda c: ["NVDA"])
    monkeypatch.setattr(orchestration, "sector_for_symbol", lambda c, sym: "semiconductor")
    monkeypatch.setattr(
        orchestration, "build_context",
        lambda c, *, symbol, sector, horizon: (_fake_ctx(symbol, entry_price=entry_price), None),
    )
    monkeypatch.setattr(orchestration, "list_active_traders", lambda c: [
        type("T", (), {"trader_id": "stub", "engine_key": "stub"})()
    ])
    monkeypatch.setattr(orchestration, "ENGINE_REGISTRY", {"stub": _StubEngine()})
    monkeypatch.setattr(orchestration, "build_option_structure", lambda c, ctx, call: dict(_STRUCTURE))


def test_legacy_immediate_mode_still_books_in_the_same_pass(monkeypatch):
    """decision_point=None (the default, every pre-P1 caller) must keep
    behaving exactly as before: recorded AND booked in one pass."""
    conn = _conn()
    _wire_stub(monkeypatch)

    summary = orchestration.run_predictions(conn)

    assert summary["stub"]["recorded"] == 1
    row = conn.execute("SELECT decision_point, confirmed_at FROM trader_predictions").fetchone()
    assert row[0] is None
    trade_count = conn.execute("SELECT count(*) FROM trader_trades").fetchone()[0]
    assert trade_count == 1  # booked immediately


def test_pre_market_call_defers_booking(monkeypatch):
    conn = _conn()
    _wire_stub(monkeypatch)

    summary = orchestration.run_predictions(conn, decision_point="pre_market")

    assert summary["stub"]["recorded"] == 1
    row = conn.execute(
        "SELECT decision_point, confirmed_at, withdrawn FROM trader_predictions"
    ).fetchone()
    assert row[0] == "pre_market"
    assert row[1] is None
    assert row[2] in (False, None)
    # option_structure was still built/stored (mechanical, at cast time)...
    stored = conn.execute("SELECT option_structure FROM trader_predictions").fetchone()[0]
    assert stored is not None
    # ...but nothing was booked yet.
    assert conn.execute("SELECT count(*) FROM trader_trades").fetchone()[0] == 0


def test_pre_market_call_also_gets_multi_horizon_grade_rows(monkeypatch):
    conn = _conn()
    _wire_stub(monkeypatch)
    orchestration.run_predictions(conn, decision_point="pre_market")

    horizons = {
        r[0] for r in conn.execute("SELECT horizon_days FROM trader_prediction_grades").fetchall()
    }
    assert horizons == {1, 5, 21}


def test_confirm_and_book_confirms_and_books_when_quote_within_band(monkeypatch):
    conn = _conn()
    _wire_stub(monkeypatch, entry_price=100.0)
    orchestration.run_predictions(conn, decision_point="pre_market")

    # A live quote barely off entry -- well within the 'up' call's own band.
    fake_quotes = lambda symbols: {"NVDA": {"price": 100.5, "prev_close": 100.0, "as_of": None}}
    summary = orchestration.confirm_and_book(conn, as_of=TRADE_DATE, fetch_quotes=fake_quotes)

    assert summary["confirmed"] == 1
    assert summary["withdrawn"] == 0
    assert summary["booked"] == 1
    row = conn.execute("SELECT confirmed_at, withdrawn FROM trader_predictions").fetchone()
    assert row[0] is not None
    assert row[1] in (False, None)
    assert conn.execute("SELECT count(*) FROM trader_trades").fetchone()[0] == 1


def test_confirm_and_book_withdraws_when_quote_already_invalidates_the_call(monkeypatch):
    """The 'up' call's own grading band at entry_price=100, grade_vol=0.4,
    band_k=0.5, horizon=5 is well under a 20% move -- a quote already down
    20% before the fill is a clear adverse move, must withdraw not book."""
    conn = _conn()
    _wire_stub(monkeypatch, entry_price=100.0)
    orchestration.run_predictions(conn, decision_point="pre_market")

    fake_quotes = lambda symbols: {"NVDA": {"price": 80.0, "prev_close": 100.0, "as_of": None}}
    summary = orchestration.confirm_and_book(conn, as_of=TRADE_DATE, fetch_quotes=fake_quotes)

    assert summary["withdrawn"] == 1
    assert summary["confirmed"] == 0
    assert summary["booked"] == 0
    row = conn.execute("SELECT confirmed_at, withdrawn, withdrawn_reason FROM trader_predictions").fetchone()
    assert row[0] is None
    assert row[1] is True
    assert "against 'up'" in row[2]
    assert conn.execute("SELECT count(*) FROM trader_trades").fetchone()[0] == 0


def test_confirm_and_book_confirms_by_default_with_no_live_quote(monkeypatch):
    conn = _conn()
    _wire_stub(monkeypatch)
    orchestration.run_predictions(conn, decision_point="pre_market")

    summary = orchestration.confirm_and_book(conn, as_of=TRADE_DATE, fetch_quotes=lambda symbols: {})

    assert summary["confirmed"] == 1
    assert summary["withdrawn"] == 0


def test_confirm_and_book_is_idempotent(monkeypatch):
    conn = _conn()
    _wire_stub(monkeypatch)
    orchestration.run_predictions(conn, decision_point="pre_market")
    fake_quotes = lambda symbols: {"NVDA": {"price": 100.5, "prev_close": 100.0, "as_of": None}}

    first = orchestration.confirm_and_book(conn, as_of=TRADE_DATE, fetch_quotes=fake_quotes)
    second = orchestration.confirm_and_book(conn, as_of=TRADE_DATE, fetch_quotes=fake_quotes)

    assert first["confirmed"] == 1
    assert second["confirmed"] == 0 and second["withdrawn"] == 0  # nothing left pending
    assert conn.execute("SELECT count(*) FROM trader_trades").fetchone()[0] == 1  # not double-booked


def test_post_open_never_creates_new_calls(monkeypatch):
    """Story 6: post_open can only confirm/withdraw, never issue a new call."""
    conn = _conn()
    _wire_stub(monkeypatch)
    orchestration.run_predictions(conn, decision_point="pre_market")
    n_before = conn.execute("SELECT count(*) FROM trader_predictions").fetchone()[0]

    orchestration.confirm_and_book(conn, as_of=TRADE_DATE, fetch_quotes=lambda symbols: {})

    n_after = conn.execute("SELECT count(*) FROM trader_predictions").fetchone()[0]
    assert n_after == n_before


def test_run_decision_point_dispatches_pre_market_to_run_predictions(monkeypatch):
    conn = _conn()
    _wire_stub(monkeypatch)
    result = orchestration.run_decision_point(conn, decision_point="pre_market", as_of=TRADE_DATE)
    assert result["stub"]["recorded"] == 1


def test_run_decision_point_dispatches_post_open_to_confirm_and_book(monkeypatch):
    conn = _conn()
    _wire_stub(monkeypatch)
    orchestration.run_predictions(conn, decision_point="event")
    result = orchestration.run_decision_point(conn, decision_point="post_open", as_of=TRADE_DATE)
    assert result["confirmed"] == 1


def test_run_decision_point_post_close_grades_reviews_and_builds_digest(monkeypatch):
    conn = _conn()
    _wire_stub(monkeypatch)
    orchestration.run_predictions(conn)  # legacy/immediate: booked, ready to mature

    monkeypatch.setattr(
        orchestration, "_price_with_grace", lambda c, symbol, label_end_date: 130.0
    )
    result = orchestration.run_decision_point(conn, decision_point="post_close", as_of=date(2026, 8, 20))

    assert result["grading"]["graded"] == 1
    assert result["grading"]["horizon_graded"][1] == 1
    assert result["grading"]["horizon_graded"][5] == 1
    assert result["grading"]["horizon_graded"][21] == 1
    assert isinstance(result["review"], dict)
    assert "需要你決定的事" in result["digest"]


def test_withdrawn_call_is_never_graded(monkeypatch):
    """Migration 040: 'A withdrawn call is never booked and is excluded from
    grading -- it never became a real opinion the trader stood behind.'"""
    conn = _conn()
    _wire_stub(monkeypatch, entry_price=100.0)
    orchestration.run_predictions(conn, decision_point="pre_market")
    orchestration.confirm_and_book(
        conn, as_of=TRADE_DATE, fetch_quotes=lambda symbols: {"NVDA": {"price": 80.0, "prev_close": 100.0, "as_of": None}}
    )
    row = conn.execute("SELECT withdrawn FROM trader_predictions").fetchone()
    assert row[0] is True

    monkeypatch.setattr(orchestration, "_price_with_grace", lambda c, symbol, label_end_date: 130.0)
    grading = orchestration.grade_matured(conn, as_of=date(2026, 8, 20))

    assert grading["graded"] == 0
    assert grading["horizon_graded"][1] == 0
    assert grading["horizon_graded"][5] == 0
    assert grading["horizon_graded"][21] == 0
    row = conn.execute("SELECT status FROM trader_predictions").fetchone()
    assert row[0] == "pending"  # never graded


def test_run_decision_point_rejects_unknown_decision_point():
    conn = _conn()
    with pytest.raises(ValueError):
        orchestration.run_decision_point(conn, decision_point="lunchtime")


# --- is_event_day (story 8) --------------------------------------------------

def _seed_event(conn, *, event_type, symbol=None, scheduled_at):
    conn.execute(
        "INSERT INTO event_calendar (event_id, symbol, event_type, scheduled_at, status, source, ingested_at) "
        "VALUES (?, ?, ?, ?, 'scheduled', 'test', ?)",
        [f"{event_type}-{symbol}-{scheduled_at}", symbol, event_type, scheduled_at, datetime.now(timezone.utc)],
    )


def test_is_event_day_true_for_macro_event():
    conn = _conn()
    _seed_event(conn, event_type="fomc", symbol=None, scheduled_at=datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc))
    assert orchestration.is_event_day(conn, date(2026, 7, 15)) is True


def test_is_event_day_true_for_watchlist_earnings():
    conn = _conn()  # NVDA is seeded onto watchlist_members by migration 001
    _seed_event(conn, event_type="earnings", symbol="NVDA", scheduled_at=datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc))
    assert orchestration.is_event_day(conn, date(2026, 7, 15)) is True


def test_is_event_day_false_on_an_ordinary_day():
    conn = _conn()
    assert orchestration.is_event_day(conn, date(2026, 7, 15)) is False


def test_is_event_day_ignores_events_on_other_days():
    conn = _conn()
    _seed_event(conn, event_type="cpi", symbol=None, scheduled_at=datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc))
    assert orchestration.is_event_day(conn, date(2026, 7, 15)) is False
