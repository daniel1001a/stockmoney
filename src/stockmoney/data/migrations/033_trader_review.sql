-- Per-trader review (the generalized A4 engine) + cross-trader divergence.
-- Both are read-only analysis side-branches (CLAUDE.md sections 8/11): they
-- read already-graded trader_predictions and produce human-facing analytics;
-- nothing here is ever read by a training path (isolation canary enforces it).
--
-- trader_review_log: the multi-trader generalization of attribution_log. One
-- row per (graded prediction) with the same factor decomposition + verdict as
-- attribution.py, but keyed by trader_id/method_version so each trader's
-- "why did I win/lose" is tracked separately. attribution_log itself is left
-- untouched (the legacy Chartist-only path keeps running); this is a parallel
-- table so its PK can include trader_id (DuckDB can't easily ALTER a PK).
--
-- trader_divergence_log: CLAUDE.md section 8 -- when two traders disagree on
-- the same (symbol, day), that disagreement is itself a first-class signal.
-- One row per (day, symbol, trader) that predicted; `disagreed` flags that at
-- least one other trader took the opposite direction that day; `was_right` is
-- back-filled after grading (who was ultimately correct). Idempotent per
-- (trade_date, symbol, trader_id).
CREATE TABLE trader_review_log (
    review_date DATE NOT NULL,          -- = the prediction's trade_date (the day being reviewed)
    symbol VARCHAR NOT NULL,
    trader_id VARCHAR NOT NULL,
    method_version VARCHAR NOT NULL,
    predicted_direction VARCHAR NOT NULL,
    predicted_confidence DOUBLE NOT NULL,
    actual_return DOUBLE NOT NULL,
    attr_macro DOUBLE NOT NULL,
    attr_sector DOUBLE NOT NULL,
    attr_idiosyncratic DOUBLE NOT NULL,
    event_tags VARCHAR,                 -- matched GDELT/news tags (CSV), '' if none
    verdict_class VARCHAR NOT NULL,     -- 'right_reason_right' | 'wrong_noise' | 'wrong_signal_existed'
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (review_date, symbol, trader_id, method_version)
);

CREATE TABLE trader_divergence_log (
    trade_date DATE NOT NULL,
    symbol VARCHAR NOT NULL,
    trader_id VARCHAR NOT NULL,
    direction VARCHAR NOT NULL,
    conviction DOUBLE NOT NULL,
    disagreed BOOLEAN NOT NULL,         -- another trader took the opposite direction that day
    was_right BOOLEAN,                  -- back-filled after grading; NULL while any side still pending
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (trade_date, symbol, trader_id)
);
