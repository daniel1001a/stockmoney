CREATE TABLE put_call_ratio_daily (
    symbol VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    put_volume BIGINT,
    call_volume BIGINT,
    put_oi BIGINT,
    call_oi BIGINT,
    source VARCHAR NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (symbol, trade_date, ingested_at)
);
