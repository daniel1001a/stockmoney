"""Tests for the migration-039 factions (Reversion / Flow / Sentiment). Same
in-memory pattern as test_engines.py; each engine reads features it queries
itself from feature_store / alt_social_hourly, so we seed those directly."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import duckdb
import numpy as np

from stockmoney.data.db import run_migrations
from stockmoney.league.context import MarketContext
from stockmoney.league.engines import (
    ENGINE_REGISTRY,
    FLOW_METHOD_VERSION,
    REVERSION_METHOD_VERSION,
    SENTIMENT_METHOD_VERSION,
    FlowEngine,
    ReversionEngine,
    SentimentEngine,
)
from stockmoney.models.production import ProductionPrediction

TRADE_DATE = date(2026, 6, 1)
END = date(2026, 6, 8)
CTX_AT = datetime(2026, 6, 1, 21, tzinfo=timezone.utc)   # context anchoring instant
PAST = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)     # available before the context -> usable
FUTURE = datetime(2026, 6, 2, 12, tzinfo=timezone.utc)   # available after -> look-ahead, must be excluded


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def _ctx(*, symbol="NVDA"):
    prod = ProductionPrediction(
        as_of_date=TRADE_DATE, symbol=symbol, sector="semiconductor", horizon=5,
        regime=0, proba=np.array([0.1, 0.2, 0.7]),
        feature_values={"realized_vol_20d": 0.4, "adx_14": 25.0, "xsec_dispersion": 0.03},
        model_version="gmm-logistic-v2",
    )
    return MarketContext(
        symbol=symbol, sector="semiconductor", trade_date=TRADE_DATE, horizon=5,
        label_end_date=END, entry_price=100.0, grade_vol=0.4, band_k=0.5,
        regime=0, available_at=CTX_AT, production=prod,
    )


def _seed_feat(conn, name, value, *, symbol="NVDA", fdate=TRADE_DATE, available_at=PAST):
    conn.execute(
        "INSERT INTO feature_store (feature_date, symbol, feature_name, feature_value, "
        "feature_version, available_at, computed_at, source_table) VALUES (?,?,?,?,?,?,?,?)",
        [fdate, symbol.upper(), name, value, "v1", available_at, available_at, "test"],
    )


# --- Reversion --------------------------------------------------------------

def test_reversion_overbought_fades_down():
    conn = _conn()
    _seed_feat(conn, "rsi_14", 80.0)
    call, skip = ReversionEngine().predict(conn, _ctx())
    assert skip is None
    assert call.direction == "down"
    assert call.method_version == REVERSION_METHOD_VERSION
    assert 0.0 <= call.conviction <= 1.0 and call.conviction > 0.5


def test_reversion_oversold_fades_up():
    conn = _conn()
    _seed_feat(conn, "rsi_14", 20.0)
    assert ReversionEngine().predict(conn, _ctx())[0].direction == "up"


def test_reversion_midrange_is_range():
    conn = _conn()
    _seed_feat(conn, "rsi_14", 50.0)
    assert ReversionEngine().predict(conn, _ctx())[0].direction == "range"


def test_reversion_skips_without_rsi():
    call, skip = ReversionEngine().predict(_conn(), _ctx())
    assert call is None and "rsi_14" in skip


def test_reversion_lookahead_guard_excludes_future_availability():
    conn = _conn()
    _seed_feat(conn, "rsi_14", 80.0, available_at=FUTURE)  # not knowable as-of the league instant
    call, skip = ReversionEngine().predict(conn, _ctx())
    assert call is None and "rsi_14" in skip


# --- Flow -------------------------------------------------------------------

def test_flow_all_bearish_votes_go_down():
    conn = _conn()
    _seed_feat(conn, "gex_estimate", -1.0)        # negative dealer gamma
    _seed_feat(conn, "skew_25delta_chg_1d", 0.5)  # downside skew steepening
    _seed_feat(conn, "put_call_ratio", 1.4)       # more puts than calls
    call, skip = FlowEngine().predict(conn, _ctx())
    assert skip is None and call.direction == "down"
    assert call.method_version == FLOW_METHOD_VERSION


def test_flow_all_bullish_votes_go_up():
    conn = _conn()
    _seed_feat(conn, "gex_estimate", 1.0)
    _seed_feat(conn, "skew_25delta_chg_1d", -0.5)
    _seed_feat(conn, "put_call_ratio", 0.5)
    assert FlowEngine().predict(conn, _ctx())[0].direction == "up"


def test_flow_skips_without_any_positioning_feature():
    call, skip = FlowEngine().predict(_conn(), _ctx())
    assert call is None and "positioning" in skip


def test_flow_populates_trade_note():
    conn = _conn()
    _seed_feat(conn, "gex_estimate", -1.0)
    _seed_feat(conn, "skew_25delta_chg_1d", 0.5)
    _seed_feat(conn, "put_call_ratio", 1.4)
    call, _ = FlowEngine().predict(conn, _ctx())
    assert call.thesis and call.evidence_chain
    assert 1 <= len(call.evidence_chain) <= 3
    assert call.evidence_chain[-1]["kind"] == "inference"
    assert call.rejected_alternatives and call.confidence_rationale


# --- Sentiment --------------------------------------------------------------

def test_sentiment_rising_market_tone_goes_up():
    conn = _conn()
    # market tone improves from -5 (5d back) to 0 (league day) -> positive heat
    _seed_feat(conn, "gdelt_avgtone_1d", 0.0, symbol="__MARKET__", fdate=TRADE_DATE)
    _seed_feat(conn, "gdelt_avgtone_1d", -5.0, symbol="__MARKET__", fdate=TRADE_DATE - timedelta(days=5))
    call, skip = SentimentEngine().predict(conn, _ctx())
    assert skip is None and call.direction == "up"
    assert call.method_version == SENTIMENT_METHOD_VERSION


def test_sentiment_falling_market_tone_goes_down():
    conn = _conn()
    _seed_feat(conn, "gdelt_avgtone_1d", -6.0, symbol="__MARKET__", fdate=TRADE_DATE)
    _seed_feat(conn, "gdelt_avgtone_1d", 0.0, symbol="__MARKET__", fdate=TRADE_DATE - timedelta(days=5))
    assert SentimentEngine().predict(conn, _ctx())[0].direction == "down"


def test_sentiment_skips_without_any_signal():
    call, skip = SentimentEngine().predict(_conn(), _ctx())
    assert call is None and "sentiment" in skip


# --- Registry ---------------------------------------------------------------

def test_all_five_factions_registered():
    assert set(ENGINE_REGISTRY) == {"chartist", "analyst", "reversion", "flow", "sentiment"}
    for key, engine in ENGINE_REGISTRY.items():
        assert engine.key == key
