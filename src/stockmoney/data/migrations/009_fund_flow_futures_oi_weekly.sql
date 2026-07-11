CREATE TABLE fund_flow_futures_oi_weekly (
    symbol VARCHAR NOT NULL,
    report_date DATE NOT NULL,
    open_interest BIGINT,
    net_noncommercial_position BIGINT,
    source VARCHAR NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (symbol, report_date, ingested_at)
);
