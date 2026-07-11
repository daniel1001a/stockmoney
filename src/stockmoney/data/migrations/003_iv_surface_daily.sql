CREATE TABLE iv_surface_daily (
    symbol VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    expiry_date DATE NOT NULL,
    delta_bucket VARCHAR NOT NULL,      -- '25c' | '25p' | '50'
    implied_vol DOUBLE,
    source VARCHAR NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (symbol, trade_date, expiry_date, delta_bucket, ingested_at)
);
