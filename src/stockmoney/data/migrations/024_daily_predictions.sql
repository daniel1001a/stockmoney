-- Daily prediction ledger: each row commits to a concrete, gradeable call
-- (direction + probability + target band) for a symbol as of a trade_date,
-- produced by stockmoney.models.production's fit-on-resolved-history /
-- infer-on-unresolved-row path (never the walk-forward backtest, which is
-- OOS-evaluation only). Grading later fills in outcome fields once
-- label_end_date has passed -- this is a two-phase ledger like
-- option_positions, not append-only, because a prediction has a pending
-- verdict at write time, unlike feature_store/attribution_log rows which are
-- already-finished facts when written.
--
-- This table is NEVER read by any training path (feature_matrix.py /
-- walk_forward.py / regime.py / direction.py never query it) -- CLAUDE.md
-- section 7's discretion/meta-layer separation: human-visible outcomes must
-- not silently re-enter training data. Grading here only produces
-- human-reviewable win-rate analytics, the same "read-only analysis layer"
-- role attribution_log plays for section 11.
CREATE TABLE daily_predictions (
    prediction_id VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    symbol VARCHAR NOT NULL,
    sector VARCHAR NOT NULL,
    horizon INTEGER NOT NULL,
    label_end_date DATE NOT NULL,
    regime INTEGER NOT NULL,
    proba_down DOUBLE NOT NULL,
    proba_range DOUBLE NOT NULL,
    proba_up DOUBLE NOT NULL,
    predicted_direction VARCHAR NOT NULL,   -- 'down' | 'range' | 'up' (argmax of the three)
    entry_price DOUBLE NOT NULL,
    band_k DOUBLE NOT NULL,                 -- same band_k feature_matrix.py used, so grading is reproducible
    target_price_up DOUBLE NOT NULL,
    target_price_down DOUBLE NOT NULL,
    feature_values VARCHAR NOT NULL,        -- JSON {feature_name: value}, the "why" panel's data source
    model_version VARCHAR NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'pending', -- 'pending' | 'graded'
    actual_price DOUBLE,
    actual_return DOUBLE,
    actual_label VARCHAR,                   -- 'down' | 'range' | 'up', filled at grading
    outcome VARCHAR,                        -- 'win' | 'loss', filled at grading
    graded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (prediction_id)
);
