-- Nightly-computed cache of the slow walk-forward backtest (module A/B) for
-- every watchlist symbol, read by the FastAPI backend so an API request never
-- blocks on fitting/backtesting live (that was the Streamlit dashboard's
-- "跑 walk-forward 回測中" spinner -- fine for a single developer-facing
-- SOXL-only diagnostic panel, not acceptable for a request/response API).
--
-- GMM only, not HMM: stockmoney.models.production already picked GMM as the
-- unattended-production default (no significant OOS Brier difference found
-- vs HMM, and HMM has a known degenerate-regime failure mode) -- this table
-- mirrors that choice rather than re-litigating it. The GMM vs HMM
-- comparison itself is still available via backtest_semiconductor.py for
-- anyone who wants to re-check it.
--
-- One row per (as_of_date, symbol): today's row is what the API reads;
-- history is kept (not overwritten) purely as an audit trail of how backtest
-- numbers drift as more data accumulates -- nothing reads past rows today.
CREATE TABLE symbol_backtest_snapshot (
    as_of_date DATE NOT NULL,
    symbol VARCHAR NOT NULL,
    sector VARCHAR NOT NULL,
    overall_n INTEGER,
    overall_accuracy DOUBLE,
    overall_brier DOUBLE,
    overall_sharpe DOUBLE,
    ev_passed_n INTEGER,
    ev_passed_win_rate DOUBLE,
    ev_blocked_n INTEGER,
    ev_blocked_win_rate DOUBLE,
    ev_of_continuing_now DOUBLE,
    computed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (as_of_date, symbol)
);
