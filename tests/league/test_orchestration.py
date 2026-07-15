"""Regression coverage for the 2026-07-15 "league predict FAILED" incident:
a NaN entry_price (from a yfinance partial-bar close slipping past `close IS
NOT NULL`, see stockmoney.data.positions.latest_underlying_price) flowed into
option_selection.select_option -> strike_ladder.snap_strike, which correctly
raised ValueError("strike must be a positive finite number, got nan") -- but
uncaught, that aborted run_predictions for every symbol in the league, not
just the bad one.

The fix has three independent layers (each covered by its own unit test
elsewhere): (1) positions.py now excludes isnan(close) at the source: see
tests/data/test_positions.py; (2) option_selection.select_option now treats a
non-finite spot/iv the same as "no usable entry IV": see
tests/models/test_option_selection.py and tests/league/test_option_bridge.py.
This module covers the third, outermost layer: orchestration.run_predictions
must never let a build_option_structure failure (this guard, or any other
future one) abort the whole league predict pass -- it must log a clear skip
reason and keep going, while still recording the trader's directional call
(just without an instrument), never storing an invalid strike.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import duckdb
import numpy as np

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


def _fake_ctx(symbol="NVDA"):
    prod = ProductionPrediction(
        as_of_date=TRADE_DATE, symbol=symbol, sector="semiconductor", horizon=5,
        regime=0, proba=np.array([0.1, 0.2, 0.7]),
        feature_values={"realized_vol_20d": 0.4}, model_version="test",
    )
    return MarketContext(
        symbol=symbol, sector="semiconductor", trade_date=TRADE_DATE, horizon=5,
        label_end_date=END, entry_price=100.0, grade_vol=0.4, band_k=0.5,
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


def test_run_predictions_skips_gracefully_when_option_structure_build_fails(monkeypatch):
    """Simulates strike_ladder's guard firing deep inside build_option_structure
    (the exact 2026-07-15 shape: a ValueError('strike must be a positive
    finite number, got nan') for one symbol). run_predictions must not raise,
    must record a skip reason naming the symbol/trader, must still record the
    trader's directional call, and must NOT store an option_structure for it
    -- and, critically, other symbols must still get processed."""
    conn = _conn()

    monkeypatch.setattr(orchestration, "_active_watchlist_symbols", lambda c: ["NVDA", "AVGO"])
    monkeypatch.setattr(orchestration, "sector_for_symbol", lambda c, sym: "semiconductor")
    monkeypatch.setattr(orchestration, "build_context", lambda c, *, symbol, sector, horizon: (_fake_ctx(symbol), None))
    monkeypatch.setattr(orchestration, "list_active_traders", lambda c: [
        type("T", (), {"trader_id": "stub", "engine_key": "stub"})()
    ])
    monkeypatch.setattr(orchestration, "ENGINE_REGISTRY", {"stub": _StubEngine()})

    def _boom(c, ctx, call):
        if ctx.symbol == "NVDA":
            raise ValueError("strike must be a positive finite number, got nan")
        return {"spot": 100.0, "strike": 105.0, "is_call": True, "iv": 0.4,
                "iv_source": "proxy", "t_years": 30 / 365, "dte_days": 30, "side": "long"}

    monkeypatch.setattr(orchestration, "build_option_structure", _boom)

    summary = orchestration.run_predictions(conn)

    # The whole pass completed -- AVGO (the "good" symbol) was still recorded.
    assert summary["stub"]["recorded"] == 2
    assert summary["stub"]["skipped"] == 0

    # A clear, attributable skip reason was logged for the failing symbol.
    matches = [s for s in summary["skips"] if "NVDA/stub" in s and "option structure build failed" in s]
    assert len(matches) == 1

    # NVDA's prediction WAS recorded (the directional call is still valid
    # data) but with no option_structure -- never an invalid strike stored.
    row = conn.execute(
        "SELECT option_structure FROM trader_predictions WHERE symbol = 'NVDA' AND trader_id = 'stub'"
    ).fetchone()
    assert row is not None
    assert row[0] is None

    # AVGO's prediction went through unaffected, with its instrument intact.
    avgo_row = conn.execute(
        "SELECT option_structure FROM trader_predictions WHERE symbol = 'AVGO' AND trader_id = 'stub'"
    ).fetchone()
    assert avgo_row[0] is not None
