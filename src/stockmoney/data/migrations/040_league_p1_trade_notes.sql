-- Issue #7 Phase 1 (交易員聯盟 P1): structured trade notes, decision-point
-- gating, quote-lag stamping, and multi-horizon grading. See CONTEXT.md for
-- the Trade Note / Decision Point / Grading Horizon vocabulary.
--
-- All trader_predictions additions are NULLABLE / default-false: existing
-- graded history stays exactly as it is (old rationale/invalidation keep
-- meaning what they always meant), only new calls populate the new columns.
-- This is additive, not a rewrite of the existing single-horizon grading --
-- see trader_prediction_grades below for the new multi-horizon path.

-- Trade Note (交易筆記): structured replacement for the two free-text fields.
-- thesis is the one-line 論點; evidence_chain is a JSON array of up to 3
-- layers ({layer, kind: 'observation'|'inference', credibility, note}), the
-- layer COUNT itself is never scored (CLAUDE.md discipline: no rewarding
-- complexity for its own sake); rejected_alternatives records the contrary
-- read a trader considered and discounted; confidence_rationale ties the
-- numeric conviction back to what actually drove it.
ALTER TABLE trader_predictions ADD COLUMN thesis VARCHAR;
ALTER TABLE trader_predictions ADD COLUMN evidence_chain VARCHAR;
ALTER TABLE trader_predictions ADD COLUMN rejected_alternatives VARCHAR;
ALTER TABLE trader_predictions ADD COLUMN confidence_rationale VARCHAR;

-- Decision Point (決策時點): which of the day's scheduled checkpoints this
-- call was made at. NULL means "legacy / immediate" -- the pre-P1 behaviour
-- where a call is recorded and booked in the same run_predictions pass, kept
-- so every existing caller (nightly_refresh, the CLI, backfill scripts,
-- existing tests) keeps working unchanged. 'pre_market' and 'event' calls are
-- new opinions; 'post_open' is never written here (that decision point only
-- confirms/withdraws same-day 'pre_market'/'event' rows, see confirmed_at/
-- withdrawn below); 'post_close' never creates a row at all.
ALTER TABLE trader_predictions ADD COLUMN decision_point VARCHAR;
-- Set by orchestration.confirm_and_book at the post_open decision point for a
-- 'pre_market'/'event' row that is still standing (not withdrawn). NULL for
-- legacy immediate calls (nothing to confirm) and for calls still awaiting
-- their post_open pass.
ALTER TABLE trader_predictions ADD COLUMN confirmed_at TIMESTAMPTZ;
-- A 'pre_market'/'event' call the post_open pass pulled instead of confirming
-- (CLAUDE.md-style "以避免追高殺低" -- the thesis was already invalidated
-- before the fill could happen). A withdrawn call is never booked and is
-- excluded from grading -- it never became a real opinion the trader stood
-- behind.
-- DuckDB's ALTER TABLE ADD COLUMN doesn't support a NOT NULL constraint, so
-- this is nullable with a DEFAULT; every read path treats NULL the same as
-- false (see data/trader_predictions.py's _row_to_prediction).
ALTER TABLE trader_predictions ADD COLUMN withdrawn BOOLEAN DEFAULT false;
ALTER TABLE trader_predictions ADD COLUMN withdrawn_at TIMESTAMPTZ;
ALTER TABLE trader_predictions ADD COLUMN withdrawn_reason VARCHAR;

-- Quote lag (報價延遲): every real fill in this ledger is priced off the same
-- yfinance free-tier feed live_quotes.py already documents as ~15min delayed.
-- Stamped on every booked trade (not just decision-point-gated ones) so any
-- future backtest/perf accounting can treat that lag as a cost honestly,
-- instead of implicitly assuming instant fills.
ALTER TABLE trader_trades ADD COLUMN quote_lag_minutes INTEGER;

-- Grading Horizon (評分視野): a call written once at cast time now gets
-- graded independently at 1, 5 and 21 trading days out, so "right on a 1-day
-- pop but wrong by 21 days" and vice versa both get recorded rather than
-- collapsed into whatever single horizon.py's original label_end_date
-- happened to pick. trader_predictions.label_end_date/status/actual_* stay
-- exactly as they are today (that is this call's ORIGINAL declared-horizon
-- grade); this table is the new, independent multi-horizon layer alongside
-- it, keyed by (prediction_id, horizon_days) so the same call can hold three
-- independent verdicts. Existing single-horizon history is NOT backfilled
-- here -- CLAUDE.md's look-ahead discipline: a grade only exists once a call
-- was actually written with that horizon in view, never invented after the
-- fact for old rows that never declared it.
CREATE TABLE trader_prediction_grades (
    prediction_id VARCHAR NOT NULL,
    horizon_days INTEGER NOT NULL,
    label_end_date DATE NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'pending',  -- 'pending' | 'graded'
    actual_price DOUBLE,
    actual_return DOUBLE,
    actual_label VARCHAR,                       -- 'down' | 'range' | 'up'
    outcome VARCHAR,                             -- 'win' | 'loss'
    graded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (prediction_id, horizon_days)
);
