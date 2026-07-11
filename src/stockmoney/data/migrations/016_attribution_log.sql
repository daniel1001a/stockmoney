-- Read-only output of the daily attribution/review engine (CLAUDE.md section 11).
-- Append-only; the engine only ever produces "new feature hypotheses",
-- never revisions to past labels.
CREATE TABLE attribution_log (
    attribution_date DATE NOT NULL,
    symbol VARCHAR NOT NULL,            -- concrete ticker/ETF; sentinel '__MARKET__' for market-level attribution
    predicted_direction VARCHAR,        -- 'up' | 'down' | 'neutral'
    predicted_confidence DOUBLE,
    actual_return DOUBLE,
    attr_macro DOUBLE,                  -- attribution: macro contribution
    attr_sector DOUBLE,                 -- sector contribution
    attr_idiosyncratic DOUBLE,          -- idiosyncratic residual contribution
    event_tags VARCHAR,                 -- matched GDELT/earnings event tags (CSV or JSON)
    verdict_class VARCHAR NOT NULL,     -- 'right_reason_right' | 'wrong_noise' | 'wrong_signal_existed'
    model_version VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (attribution_date, symbol, model_version)
);
