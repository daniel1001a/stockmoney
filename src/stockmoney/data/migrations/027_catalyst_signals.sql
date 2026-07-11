-- Output of the second (Sonnet, deep) pass of catalyst reasoning
-- (~/.claude/plans/frontend-catalyst-rebuild.md Track A / A2): "what has the
-- market not priced in yet" for a watchlist symbol, built from the first
-- (Haiku) pass's aggregated sentiment/candidate signals
-- (stockmoney.data.scan_classify) plus recent price/IV action.
--
-- CLAUDE.md section 1's discipline applies directly here: this is a
-- discretion-layer input, never a model feature. Nothing in
-- stockmoney.models reads this table -- it's surfaced to the human
-- alongside module A/B's output for the human to weigh, not blended into
-- either model's probability output (see the plan doc's rule #1: new
-- alt-data signals need a passed bootstrap significance test, per CLAUDE.md
-- section 12, before they can influence a model -- there's nowhere near
-- enough catalyst_signals history yet for that to even be attempted).
--
-- available_at is the look-ahead-bias guard (CLAUDE.md section 2's data
-- integrity rule): the timestamp this row was actually generated, not the
-- catalyst's real-world event time. Any future backtest of "did the
-- catalyst layer help" must gate on available_at <= decision time, exactly
-- like feature_store's available_at does for model features.
--
-- Idempotent per (symbol, as_of_date): re-running the synthesis pass twice
-- in one day returns the existing row rather than duplicating it (same
-- pattern as daily_predictions.record_prediction).
CREATE TABLE catalyst_signals (
    signal_id VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    as_of_date DATE NOT NULL,
    catalyst_summary VARCHAR NOT NULL,
    transmission_chain VARCHAR NOT NULL,   -- catalyst -> chain of reasoning -> affected symbol
    novelty_score DOUBLE,                  -- 0 (stale/well-known) .. 1 (fresh/surprising)
    sentiment_score DOUBLE,                -- -1 (bearish) .. 1 (bullish)
    priced_in_estimate DOUBLE,             -- 0 (not priced in yet) .. 1 (fully priced in already)
    source_refs VARCHAR,                   -- JSON array of news_articles_raw/social_posts_raw ids
    model_version VARCHAR NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (signal_id)
);
