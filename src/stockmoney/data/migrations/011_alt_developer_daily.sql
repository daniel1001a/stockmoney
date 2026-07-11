CREATE TABLE alt_developer_daily (
    symbol VARCHAR NOT NULL,
    repo VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    stars BIGINT,
    commits BIGINT,
    star_velocity DOUBLE,
    source VARCHAR NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (symbol, repo, trade_date, ingested_at)
);
