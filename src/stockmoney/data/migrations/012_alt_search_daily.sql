CREATE TABLE alt_search_daily (
    keyword VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    trends_index DOUBLE,
    source VARCHAR NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (keyword, trade_date, ingested_at)
);
