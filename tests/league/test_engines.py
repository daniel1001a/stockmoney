from __future__ import annotations

from datetime import date, datetime

import duckdb
import numpy as np

from stockmoney.data.catalyst_signals import record_catalyst_signal
from stockmoney.data.db import run_migrations
from stockmoney.league.context import MarketContext
from stockmoney.league.engines import (
    ANALYST_METHOD_VERSION,
    CHARTIST_METHOD_VERSION,
    AnalystEngine,
    ChartistEngine,
)
from stockmoney.models.production import ProductionPrediction

TRADE_DATE = date(2026, 6, 1)
END = date(2026, 6, 8)


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def _ctx(*, symbol="NVDA", proba=(0.1, 0.2, 0.7), regime=0):
    prod = ProductionPrediction(
        as_of_date=TRADE_DATE, symbol=symbol, sector="semiconductor", horizon=5,
        regime=regime, proba=np.array(proba, dtype=float),
        feature_values={"realized_vol_20d": 0.4, "adx_14": 25.0, "xsec_dispersion": 0.03},
        model_version="gmm-logistic-v2",
    )
    return MarketContext(
        symbol=symbol, sector="semiconductor", trade_date=TRADE_DATE, horizon=5,
        label_end_date=END, entry_price=100.0, grade_vol=0.4, band_k=0.5,
        regime=regime, available_at=datetime.now().astimezone(), production=prod,
    )


# --- Chartist ---------------------------------------------------------------

def test_chartist_maps_argmax_and_maxproba():
    conn = _conn()
    call, skip = ChartistEngine().predict(conn, _ctx(proba=(0.1, 0.2, 0.7)))
    assert skip is None
    assert call.direction == "up" and call.conviction == 0.7
    assert call.method_version == CHARTIST_METHOD_VERSION
    assert call.engine_payload["regime"] == 0
    assert call.engine_payload["proba_up"] == 0.7


def test_chartist_down_when_down_dominates():
    conn = _conn()
    call, _ = ChartistEngine().predict(conn, _ctx(proba=(0.6, 0.3, 0.1)))
    assert call.direction == "down" and call.conviction == 0.6


# --- Analyst ----------------------------------------------------------------

def _put_catalyst(conn, *, symbol="NVDA", as_of, sentiment, novelty=0.8, priced_in=0.2):
    record_catalyst_signal(
        conn, symbol=symbol, as_of_date=as_of, catalyst_summary="new chip demand",
        transmission_chain="demand -> revenue -> price", novelty_score=novelty,
        sentiment_score=sentiment, priced_in_estimate=priced_in, model_version="test",
    )


def test_analyst_skips_when_no_catalyst():
    conn = _conn()
    call, skip = AnalystEngine().predict(conn, _ctx())
    assert call is None and "no catalyst_signals" in skip


def test_analyst_maps_bullish_sentiment_to_up():
    conn = _conn()
    _put_catalyst(conn, as_of=TRADE_DATE, sentiment=0.5, novelty=0.8, priced_in=0.2)
    call, skip = AnalystEngine().predict(conn, _ctx())
    assert skip is None
    assert call.direction == "up"
    assert call.conviction == 0.8 * (1 - 0.2) * 0.5  # novelty*(1-priced_in)*|sentiment|
    assert call.method_version == ANALYST_METHOD_VERSION


def test_analyst_maps_bearish_to_down_and_weak_to_range():
    conn = _conn()
    _put_catalyst(conn, as_of=TRADE_DATE, sentiment=-0.6)
    assert AnalystEngine().predict(conn, _ctx())[0].direction == "down"

    conn2 = _conn()
    _put_catalyst(conn2, as_of=TRADE_DATE, sentiment=0.05)  # below EPS
    assert AnalystEngine().predict(conn2, _ctx())[0].direction == "range"


def test_analyst_lookahead_guard_skips_future_catalyst():
    conn = _conn()
    _put_catalyst(conn, as_of=date(2026, 6, 3), sentiment=0.5)  # after the league day
    call, skip = AnalystEngine().predict(conn, _ctx())
    assert call is None and "look-ahead" in skip


def test_analyst_skips_stale_catalyst():
    conn = _conn()
    _put_catalyst(conn, as_of=date(2026, 5, 1), sentiment=0.5)  # >7d before league day
    call, skip = AnalystEngine().predict(conn, _ctx())
    assert call is None and "stale" in skip


def test_analyst_treats_scraped_text_as_pure_data():
    """Injection safety: a malicious catalyst summary flows into rationale as a
    plain string; it never executes and the schema is untouched."""
    conn = _conn()
    record_catalyst_signal(
        conn, symbol="NVDA", as_of_date=TRADE_DATE,
        catalyst_summary="'); DROP TABLE trader_predictions; --",
        transmission_chain="x", novelty_score=0.5, sentiment_score=0.5,
        priced_in_estimate=0.1, model_version="test",
    )
    call, _ = AnalystEngine().predict(conn, _ctx())
    assert "DROP TABLE" in call.rationale  # carried verbatim as data
    # tables still exist
    conn.execute("SELECT count(*) FROM trader_predictions")
