-- Unified Trader League ledger: one gradeable, concrete daily call per
-- (trader, symbol, trade_date). This is the multi-trader generalization of
-- daily_predictions -- the existing single-engine daily_predictions table +
-- its grade/attribution keep running unchanged; the league starts fresh here
-- and NEVER back-fills or re-grades old daily_predictions rows (walk-forward
-- rule: new machinery must not retroactively re-score the past).
--
-- Two-phase like daily_predictions/option_positions: a row is 'pending' at
-- write time and gets its outcome fields filled once label_end_date passes.
--
-- Every trader outputs the SAME shape (direction/conviction/rationale/
-- horizon/invalidation) so the league table can score and compare them.
-- Engine-specific detail (Chartist's 3-class proba + feature_values; Analyst's
-- novelty/sentiment/priced_in/transmission chain) lives in engine_payload JSON,
-- not in typed columns, so adding a new trader with a different engine needs no
-- schema change.
--
-- GRADING IS A MARKET FACT, NOT A TRADER PROPERTY: actual_label (up/range/
-- down) is computed from (entry_price, band_k, grade_vol=realized_vol_20d,
-- horizon) using the exact same volatility-band formula
-- daily_predictions/feature_matrix use. grade_vol is a market feature every
-- trader can see, so all traders are graded on one identical ruler -- that's
-- what makes hit-rate/Brier/PnL comparable across traders.
--
-- available_at is the look-ahead guard (CLAUDE.md section 2): the moment this
-- call was actually made. Any future backtest of the league must gate on it.
--
-- ISOLATION (CLAUDE.md section 7): this table is NEVER read by any training
-- path (feature_matrix / walk_forward / regime / direction / production). A
-- static-scan canary test enforces it, same as daily_predictions/attribution.
CREATE TABLE trader_predictions (
    prediction_id VARCHAR NOT NULL,
    trader_id VARCHAR NOT NULL,
    method_version VARCHAR NOT NULL,     -- pins the trader's method version (evolution discipline)
    trade_date DATE NOT NULL,
    symbol VARCHAR NOT NULL,
    sector VARCHAR NOT NULL,
    horizon INTEGER NOT NULL,
    label_end_date DATE NOT NULL,
    direction VARCHAR NOT NULL,          -- 'up' | 'down' | 'range'
    conviction DOUBLE NOT NULL,          -- 0..1; also read as P(chosen direction correct) for Brier
    rationale VARCHAR NOT NULL,          -- plain-language why (Analyst's derives from scraped text -> pure data)
    invalidation VARCHAR NOT NULL,       -- what evidence would overturn this call
    regime INTEGER,                      -- market regime as-of trade_date (uniformly stamped so per-regime is comparable); NULL if unavailable
    entry_price DOUBLE NOT NULL,
    band_k DOUBLE NOT NULL,              -- same band_k feature_matrix uses, so grading is reproducible
    grade_vol DOUBLE NOT NULL,           -- realized_vol_20d used for the band = the market fact all traders grade on
    engine_payload VARCHAR NOT NULL,     -- JSON, engine-specific detail (proba/feature_values | novelty/sentiment/...)
    available_at TIMESTAMPTZ NOT NULL,   -- T-1 look-ahead pin
    status VARCHAR NOT NULL DEFAULT 'pending',  -- 'pending' | 'graded'
    actual_price DOUBLE,
    actual_return DOUBLE,
    actual_label VARCHAR,                -- 'down' | 'range' | 'up', filled at grading
    outcome VARCHAR,                     -- 'win' | 'loss', filled at grading
    graded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (prediction_id),
    -- one call per trader/symbol/day: idempotent re-runs of `predict` don't double-count
    UNIQUE (trader_id, symbol, trade_date)
);
