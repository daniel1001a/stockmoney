-- Layer 2: unified feature store. Model training/backtest code must query
-- only this table (filtered on available_at) and never read raw tables directly,
-- so look-ahead-bias protection lives in one place.
CREATE TABLE feature_store (
    feature_date DATE NOT NULL,
    symbol VARCHAR NOT NULL,            -- use sentinel '__MARKET__' for market-wide features (e.g. regime label)
    feature_name VARCHAR NOT NULL,
    feature_value DOUBLE,
    feature_version VARCHAR NOT NULL,   -- version of the feature computation logic
    available_at TIMESTAMPTZ NOT NULL,  -- when this value was actually knowable at the time
    computed_at TIMESTAMPTZ NOT NULL,   -- when the pipeline wrote this row (may lag available_at)
    source_table VARCHAR,               -- provenance, e.g. 'ohlcv_daily', 'macro_series_daily'
    PRIMARY KEY (feature_date, symbol, feature_name, feature_version)
);
