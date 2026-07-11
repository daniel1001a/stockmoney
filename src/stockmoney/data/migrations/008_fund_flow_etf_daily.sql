CREATE TABLE fund_flow_etf_daily (
    symbol VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    net_flow_usd DOUBLE,
    source VARCHAR NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (symbol, trade_date, ingested_at)
);
