-- Audit log for every crawl/batch ingestion job. Supports honest backtesting
-- (CLAUDE.md section 2 data-integrity rule) and detecting data-coverage gaps.
CREATE TABLE ingestion_runs (
    run_id VARCHAR NOT NULL,
    source VARCHAR NOT NULL,            -- 'yfinance' | 'fred' | 'reddit' | 'gdelt' ...
    target_table VARCHAR NOT NULL,
    window_start DATE,                  -- data coverage window (not execution time)
    window_end DATE,
    rows_written BIGINT,
    status VARCHAR NOT NULL,            -- 'success' | 'partial' | 'failed'
    error_message VARCHAR,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    PRIMARY KEY (run_id)
);
