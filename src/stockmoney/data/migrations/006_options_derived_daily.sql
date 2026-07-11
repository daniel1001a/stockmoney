CREATE TABLE options_derived_daily (
    symbol VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    metric_name VARCHAR NOT NULL,       -- 'gex_estimate' | 'skew_25delta_chg_rate' ...
    metric_value DOUBLE,
    method_version VARCHAR NOT NULL,    -- e.g. 'v1_oi_iv_approx'
    source VARCHAR NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (symbol, trade_date, metric_name, method_version, ingested_at)
);
