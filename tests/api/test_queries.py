from datetime import date, datetime, timedelta, timezone

import duckdb
import polars as pl
import pytest

from stockmoney.api import queries
from stockmoney.data.daily_predictions import grade_prediction, record_prediction
from stockmoney.data.db import append_rows, run_migrations
from stockmoney.data.positions import open_position


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def _seed_ohlcv(conn, symbol="NVDA", price=180.0, d=date(2026, 7, 9)):
    append_rows(conn, "ohlcv_daily", pl.DataFrame({
        "symbol": [symbol], "trade_date": [d], "close": [price],
        "source": ["test"], "ingested_at": [datetime.now(timezone.utc)],
    }))


def _seed_prediction(conn, symbol="NVDA", sector="semiconductor", trade_date=date(2026, 7, 9)):
    return record_prediction(
        conn, trade_date=trade_date, symbol=symbol, sector=sector, horizon=5,
        label_end_date=date(2026, 7, 16), regime=1, proba=(0.2, 0.3, 0.5),
        entry_price=180.0, feature_values={"realized_vol_20d": 0.3, "adx_14": 20.0, "xsec_dispersion": 0.01,
                                            "yield_curve_10y2y": 0.5, "dxy_chg_1d": 0.0, "oil_chg_1d": 0.0},
        model_version="test-v1",
    )


def _seed_snapshot(conn, symbol="NVDA", sector="semiconductor", as_of=date(2026, 7, 9)):
    conn.execute(
        """
        INSERT INTO symbol_backtest_snapshot (
            as_of_date, symbol, sector, overall_n, overall_accuracy, overall_brier,
            overall_sharpe, ev_passed_n, ev_passed_win_rate, ev_blocked_n,
            ev_blocked_win_rate, ev_of_continuing_now, computed_at
        ) VALUES (?, ?, ?, 100, 0.4, 0.65, 0.1, 20, 0.55, 30, 0.3, 0.01, ?)
        """,
        [as_of, symbol, sector, datetime.now(timezone.utc)],
    )


# --- pipeline_health / watchlist -----------------------------------------

def test_watchlist_core_returns_active_members():
    conn = _conn()
    core = queries.watchlist_core(conn)
    assert len(core) == 31
    assert {"symbol": "NVDA", "sector": "semiconductor", "tier": "core"} in core


def test_watchlist_candidates_empty_by_default():
    conn = _conn()
    assert queries.watchlist_candidates(conn) == []


def _seed_ingestion_run(conn, *, target_table, status="success", started_at, source="test", finished_at=None):
    conn.execute(
        """
        INSERT INTO ingestion_runs (run_id, source, target_table, status, started_at, finished_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [f"{target_table}-{started_at.isoformat()}", source, target_table, status, started_at, finished_at],
    )


def test_pipeline_health_empty_when_no_ingestion_runs():
    conn = _conn()
    assert queries.pipeline_health(conn) == []


def test_pipeline_health_flags_stale_table_past_its_lag_threshold():
    conn = _conn()
    now = datetime(2026, 7, 11, tzinfo=timezone.utc)
    # ohlcv_daily's threshold is 3 days; this run is 5 days old -> stale.
    _seed_ingestion_run(conn, target_table="ohlcv_daily", started_at=now - timedelta(days=5))
    [entry] = queries.pipeline_health(conn, now=now)
    assert entry["days_since_last_run"] == 5
    assert entry["is_stale"] is True


def test_pipeline_health_fresh_table_not_flagged():
    conn = _conn()
    now = datetime(2026, 7, 11, tzinfo=timezone.utc)
    _seed_ingestion_run(conn, target_table="ohlcv_daily", started_at=now - timedelta(days=1))
    [entry] = queries.pipeline_health(conn, now=now)
    assert entry["is_stale"] is False


def test_pipeline_health_failed_run_is_stale_even_if_recent():
    conn = _conn()
    now = datetime(2026, 7, 11, tzinfo=timezone.utc)
    _seed_ingestion_run(conn, target_table="ohlcv_daily", status="failed", started_at=now)
    [entry] = queries.pipeline_health(conn, now=now)
    assert entry["is_stale"] is True


def test_pipeline_health_table_with_no_recurring_schedule_never_stale():
    conn = _conn()
    now = datetime(2026, 7, 11, tzinfo=timezone.utc)
    # event_news_gdelt is a one-time BigQuery backfill with no recurring
    # ingester -- an old run here must never be flagged stale.
    _seed_ingestion_run(conn, target_table="event_news_gdelt", started_at=now - timedelta(days=90))
    [entry] = queries.pipeline_health(conn, now=now)
    assert entry["days_since_last_run"] == 90
    assert entry["is_stale"] is False


def test_pipeline_health_unknown_table_uses_default_lag_threshold():
    conn = _conn()
    now = datetime(2026, 7, 11, tzinfo=timezone.utc)
    _seed_ingestion_run(conn, target_table="some_new_table", started_at=now - timedelta(days=10))
    [entry] = queries.pipeline_health(conn, now=now)
    assert entry["is_stale"] is True


# --- news_freshness --------------------------------------------------------

def test_news_freshness_empty_db_degrades_to_nulls():
    conn = _conn()
    result = queries.news_freshness(conn)
    assert result["last_updated"] is None
    assert result["last_run"] is None
    assert result["news_last_24h"] == 0
    assert result["median_ingest_gap_minutes"] is None


def _seed_news_item(conn, *, item_id, symbol="NVDA", created_at, published_at=None):
    conn.execute(
        """
        INSERT INTO news_items (
            item_id, symbol, item_type, headline, published_at, available_at, created_at
        ) VALUES (?, ?, 'headline', 'test headline', ?, ?, ?)
        """,
        [item_id, symbol, published_at or created_at, created_at, created_at],
    )


def test_news_freshness_reports_last_updated_and_24h_count():
    conn = _conn()
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    _seed_news_item(conn, item_id="n1", created_at=now - timedelta(hours=2))
    _seed_news_item(conn, item_id="n2", created_at=now - timedelta(hours=40))
    result = queries.news_freshness(conn, now=now)
    assert result["last_updated"] == now - timedelta(hours=2)
    assert result["news_last_24h"] == 1


def test_news_freshness_reports_last_ingestion_run():
    conn = _conn()
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    _seed_ingestion_run(
        conn, target_table="news_articles_raw", source="rss",
        started_at=now - timedelta(hours=1), finished_at=now - timedelta(hours=1),
    )
    result = queries.news_freshness(conn, now=now)
    assert result["last_run"]["source"] == "rss"
    assert result["last_run"]["status"] == "success"


def test_news_freshness_median_gap_needs_at_least_two_runs():
    conn = _conn()
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    _seed_ingestion_run(
        conn, target_table="news_articles_raw", source="rss",
        started_at=now - timedelta(hours=1), finished_at=now - timedelta(hours=1),
    )
    result = queries.news_freshness(conn, now=now)
    assert result["median_ingest_gap_minutes"] is None


def test_news_freshness_computes_median_gap_across_runs():
    conn = _conn()
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    # Three successful runs 30 minutes apart -> median gap is 30 minutes.
    for i, hours_ago in enumerate([2.0, 1.5, 1.0]):
        started = now - timedelta(hours=hours_ago)
        _seed_ingestion_run(
            conn, target_table="news_articles_raw", source="rss",
            started_at=started, finished_at=started,
        )
    result = queries.news_freshness(conn, now=now)
    assert result["median_ingest_gap_minutes"] == pytest.approx(30.0)


# --- opportunities ---------------------------------------------------------

def test_opportunities_includes_prediction_and_snapshot():
    conn = _conn()
    _seed_prediction(conn)
    _seed_snapshot(conn)

    items = queries.opportunities(conn)
    assert len(items) == 1
    item = items[0]
    assert item["symbol"] == "NVDA"
    assert item["predicted_direction"] == "up"
    assert item["conviction"] == 0.5
    assert item["backtest"]["overall_accuracy"] == 0.4


def test_opportunities_prediction_without_snapshot_has_none_backtest():
    conn = _conn()
    _seed_prediction(conn)
    items = queries.opportunities(conn)
    assert items[0]["backtest"] is None


def _fv():
    return {"realized_vol_20d": 0.3, "adx_14": 20.0, "xsec_dispersion": 0.01,
            "yield_curve_10y2y": 0.5, "dxy_chg_1d": 0.0, "oil_chg_1d": 0.0}


def test_opportunities_directional_calls_ranked_by_directional_conviction():
    conn = _conn()
    record_prediction(
        conn, trade_date=date(2026, 7, 9), symbol="NVDA", sector="semiconductor", horizon=5,
        label_end_date=date(2026, 7, 16), regime=1, proba=(0.1, 0.2, 0.7),  # up, dir_conv 0.7
        entry_price=100.0, feature_values=_fv(), model_version="test-v1",
    )
    record_prediction(
        conn, trade_date=date(2026, 7, 9), symbol="AMD", sector="semiconductor", horizon=5,
        label_end_date=date(2026, 7, 16), regime=1, proba=(0.05, 0.1, 0.85),  # up, dir_conv 0.85
        entry_price=100.0, feature_values=_fv(), model_version="test-v1",
    )
    items = queries.opportunities(conn)
    assert [it["symbol"] for it in items] == ["AMD", "NVDA"]


def test_opportunities_high_conviction_range_sinks_below_directional_call():
    """The core 精選 fix: a high-conviction RANGE call (confidence in going
    nowhere = no options money) must never outrank a lower-conviction but
    genuinely directional call."""
    conn = _conn()
    record_prediction(
        conn, trade_date=date(2026, 7, 9), symbol="MSFT", sector="big_tech", horizon=5,
        label_end_date=date(2026, 7, 16), regime=1, proba=(0.05, 0.90, 0.05),  # range, conv 0.90
        entry_price=100.0, feature_values=_fv(), model_version="test-v1",
    )
    record_prediction(
        conn, trade_date=date(2026, 7, 9), symbol="AMD", sector="semiconductor", horizon=5,
        label_end_date=date(2026, 7, 16), regime=1, proba=(0.05, 0.40, 0.55),  # up, dir_conv 0.55
        entry_price=100.0, feature_values=_fv(), model_version="test-v1",
    )
    items = queries.opportunities(conn)
    # AMD (directional, conv 0.55) leads MSFT (range, conv 0.90) despite lower raw conviction.
    assert [it["symbol"] for it in items] == ["AMD", "MSFT"]
    amd, msft = items[0], items[1]
    assert amd["actionable"] is True and amd["directional_conviction"] == pytest.approx(0.55)
    assert msft["actionable"] is False and msft["directional_conviction"] is None


def test_opportunities_uses_only_the_latest_row_per_symbol():
    conn = _conn()
    _seed_prediction(conn, trade_date=date(2026, 7, 8))
    _seed_prediction(conn, trade_date=date(2026, 7, 9))
    items = queries.opportunities(conn)
    assert len(items) == 1
    assert items[0]["trade_date"] == date(2026, 7, 9)


# --- ticker_detail -----------------------------------------------------

def test_ticker_detail_returns_none_for_unknown_symbol():
    conn = _conn()
    assert queries.ticker_detail(conn, "ZZZZ") is None


def test_ticker_detail_includes_history_and_price_history():
    conn = _conn()
    _seed_prediction(conn)
    _seed_snapshot(conn)
    _seed_ohlcv(conn)

    detail = queries.ticker_detail(conn, "nvda")  # lowercase input normalized
    assert detail["symbol"] == "NVDA"
    assert len(detail["history"]) == 1
    assert detail["price_history"] == [{"trade_date": date(2026, 7, 9), "close": 180.0}]


def test_ticker_detail_history_reflects_graded_outcome():
    conn = _conn()
    pred_id = _seed_prediction(conn)
    grade_prediction(conn, pred_id, actual_price=190.0)

    detail = queries.ticker_detail(conn, "NVDA")
    assert detail["history"][0]["status"] == "graded"
    assert detail["history"][0]["outcome"] == "win"  # predicted up, price rose


# --- predictions_overview -----------------------------------------------

def test_predictions_overview_empty_state():
    conn = _conn()
    overview = queries.predictions_overview(conn)
    assert overview["rolling_win_rate"] == []
    assert overview["outcome_counts"] == {}
    assert overview["recent"] == []


def test_predictions_overview_after_grading():
    conn = _conn()
    pred_id = _seed_prediction(conn)
    grade_prediction(conn, pred_id, actual_price=190.0)

    overview = queries.predictions_overview(conn)
    assert overview["rolling_win_rate"] == [{"label_end_date": date(2026, 7, 16), "rolling_win_rate": 1.0}]
    assert overview["outcome_counts"] == {"win": 1}
    assert len(overview["recent"]) == 1
    assert overview["recent"][0]["symbol"] == "NVDA"


# --- positions_with_risk -------------------------------------------------

def test_positions_with_risk_empty_when_no_open_positions():
    conn = _conn()
    assert queries.positions_with_risk(conn) == []


def test_positions_with_risk_uses_cached_regime_and_ev():
    conn = _conn()
    _seed_ohlcv(conn, symbol="NVDA", price=185.0, d=date(2026, 7, 9))
    _seed_prediction(conn)   # regime=1 for NVDA
    _seed_snapshot(conn)     # ev_of_continuing_now=0.01 for NVDA

    open_position(
        conn, symbol="NVDA", option_right="call", side="long", strike=190.0,
        expiry_date=date(2026, 9, 18), entry_date=date(2026, 7, 1),
        entry_underlying_price=175.0, entry_premium=8.0, entry_iv=0.5, regime_at_entry=1,
    )

    out = queries.positions_with_risk(conn)
    assert len(out) == 1
    assert out[0]["symbol"] == "NVDA"
    assert out[0]["current_underlying_price"] == 185.0
    assert out[0]["light"] in ("green", "yellow", "red")


def test_positions_with_risk_missing_cache_data_does_not_crash():
    conn = _conn()
    open_position(
        conn, symbol="NVDA", option_right="call", side="long", strike=190.0,
        expiry_date=date(2026, 9, 18), entry_date=date(2026, 7, 1),
        entry_underlying_price=175.0, entry_premium=8.0,
    )
    out = queries.positions_with_risk(conn)
    assert len(out) == 1
    assert out[0]["light"] == "green"  # nothing evaluable yet, no triggers fire


# --- catalysts -------------------------------------------------------------

def _record_catalyst(conn, symbol="NVDA", **overrides):
    from stockmoney.data.catalyst_signals import record_catalyst_signal

    kwargs = dict(
        symbol=symbol, as_of_date=date(2026, 7, 10),
        catalyst_summary="capex commentary accelerating",
        transmission_chain="hyperscaler capex -> GPU demand -> revenue",
        novelty_score=0.8, sentiment_score=0.5, priced_in_estimate=0.2,
        source_refs=["a1"], model_version="test-v1",
    )
    kwargs.update(overrides)
    return record_catalyst_signal(conn, **kwargs)


def test_catalysts_available_true_with_no_data_yet():
    conn = _conn()
    result = queries.catalysts(conn)
    assert result["available"] is True
    assert result["items"] == []


def test_catalysts_returns_recorded_signals():
    conn = _conn()
    _record_catalyst(conn)
    result = queries.catalysts(conn)
    assert result["available"] is True
    assert len(result["items"]) == 1
    assert result["items"][0]["symbol"] == "NVDA"
    assert result["items"][0]["catalyst_summary"] == "capex commentary accelerating"


def test_opportunities_includes_catalyst_headline_when_available():
    conn = _conn()
    _seed_prediction(conn)
    _record_catalyst(conn, catalyst_summary="short headline")
    items = queries.opportunities(conn)
    assert items[0]["catalyst_headline"] == "short headline"


def test_opportunities_catalyst_headline_none_when_absent():
    conn = _conn()
    _seed_prediction(conn)
    items = queries.opportunities(conn)
    assert items[0]["catalyst_headline"] is None


def test_ticker_detail_includes_catalyst_when_available():
    conn = _conn()
    _seed_prediction(conn)
    _record_catalyst(conn, transmission_chain="a -> b -> NVDA")
    detail = queries.ticker_detail(conn, "NVDA")
    assert detail["catalyst"]["transmission_chain"] == "a -> b -> NVDA"


def test_ticker_detail_catalyst_none_when_absent():
    conn = _conn()
    _seed_prediction(conn)
    detail = queries.ticker_detail(conn, "NVDA")
    assert detail["catalyst"] is None


def _seed_prediction_regime(conn, symbol, regime, obs, trade_date=date(2026, 7, 9)):
    """Seed a prediction whose stored feature_values carry the regime-observation
    features, so regime_label_map can recover the cluster's empirical centroid."""
    fv = {
        "realized_vol_20d": obs[0], "adx_14": obs[1], "xsec_dispersion": obs[2],
        "yield_curve_10y2y": 0.5, "dxy_chg_1d": 0.0, "oil_chg_1d": 0.0,
    }
    return record_prediction(
        conn, trade_date=trade_date, symbol=symbol, sector="semiconductor", horizon=5,
        label_end_date=date(2026, 7, 16), regime=regime, proba=(0.2, 0.3, 0.5),
        entry_price=180.0, feature_values=fv, model_version="test-v1",
    )


def test_regime_label_map_from_empirical_centroids():
    conn = _conn()
    # calm cluster (id 0), stormy cluster (id 2) -- ids arbitrary, labels follow
    # the centroid.
    _seed_prediction_regime(conn, "NVDA", 0, (0.12, 12.0, 0.3), date(2026, 7, 7))
    _seed_prediction_regime(conn, "AMD", 0, (0.16, 15.0, 0.4), date(2026, 7, 8))
    _seed_prediction_regime(conn, "TSM", 2, (0.45, 35.0, 1.1), date(2026, 7, 9))
    label_map = queries.regime_label_map(conn)
    assert label_map[0] == "低波動震盪盤"
    assert label_map[2] == "高波動趨勢盤"


def test_regime_label_falls_back_for_unknown_id():
    assert queries.regime_label(None) == "未分類"
    assert queries.regime_label(7, {0: "低波動震盪盤"}) == "regime 7"
    assert queries.regime_label(0, {0: "低波動震盪盤"}) == "低波動震盪盤"


def test_regime_label_map_empty_when_no_predictions():
    conn = _conn()
    assert queries.regime_label_map(conn) == {}


# --- analyst sentiment aggregation -----------------------------------------

def _seed_analyst_rating(
    conn, *, item_id, symbol, sentiment_score, published_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
):
    conn.execute(
        """
        INSERT INTO news_items
            (item_id, symbol, item_type, headline, published_at, sentiment_score, available_at, created_at)
        VALUES (?, ?, 'analyst_rating', ?, ?, ?, ?, ?)
        """,
        [item_id, symbol, f"rating for {symbol}", published_at, sentiment_score, published_at, published_at],
    )


def test_analyst_sentiment_insufficient_data_reports_none_not_zero():
    conn = _conn()
    _seed_analyst_rating(conn, item_id="a1", symbol="NVDA", sentiment_score=0.6)
    # Only 1 scored rating -- below ANALYST_SENTIMENT_MIN_RATINGS (2).
    result = queries.analyst_sentiment_for_symbol(conn, "NVDA")
    assert result["sufficient_data"] is False
    assert result["avg_sentiment"] is None  # never fabricate "neutral" (0.0) from thin data
    assert result["n_ratings"] == 1
    assert result["latest_headline"] == "rating for NVDA"


def test_analyst_sentiment_averages_once_enough_ratings():
    conn = _conn()
    _seed_analyst_rating(conn, item_id="a1", symbol="NVDA", sentiment_score=0.6,
                          published_at=datetime(2026, 7, 8, tzinfo=timezone.utc))
    _seed_analyst_rating(conn, item_id="a2", symbol="NVDA", sentiment_score=0.2,
                          published_at=datetime(2026, 7, 10, tzinfo=timezone.utc))
    result = queries.analyst_sentiment_for_symbol(conn, "NVDA")
    assert result["sufficient_data"] is True
    assert result["avg_sentiment"] == pytest.approx(0.4)
    assert result["n_scored"] == 2
    assert result["latest_headline"] == "rating for NVDA"  # most recent by published_at


def test_analyst_sentiment_ignores_unrelated_symbol_and_old_ratings():
    conn = _conn()
    _seed_analyst_rating(conn, item_id="a1", symbol="AMD", sentiment_score=0.6)
    _seed_analyst_rating(conn, item_id="a2", symbol="NVDA", sentiment_score=0.6,
                          published_at=datetime(2020, 1, 1, tzinfo=timezone.utc))  # outside window
    result = queries.analyst_sentiment_for_symbol(conn, "NVDA", window_days=30)
    assert result["n_ratings"] == 0
    assert result["sufficient_data"] is False


def test_market_analyst_sentiment_excludes_thin_coverage_symbols():
    conn = _conn()
    # NVDA: 2 ratings, net bullish -> counted.
    _seed_analyst_rating(conn, item_id="a1", symbol="NVDA", sentiment_score=0.5,
                          published_at=datetime(2026, 7, 8, tzinfo=timezone.utc))
    _seed_analyst_rating(conn, item_id="a2", symbol="NVDA", sentiment_score=0.3,
                          published_at=datetime(2026, 7, 10, tzinfo=timezone.utc))
    # AMD: only 1 rating -> excluded entirely, not counted as neutral.
    _seed_analyst_rating(conn, item_id="a3", symbol="AMD", sentiment_score=-0.8)

    result = queries.market_analyst_sentiment(conn)
    assert result["n_symbols_covered"] == 1
    assert result["bullish"] == 1
    assert result["bearish"] == 0
    assert result["neutral"] == 0
