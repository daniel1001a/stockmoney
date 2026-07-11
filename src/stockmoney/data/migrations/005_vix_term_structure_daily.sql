CREATE TABLE vix_term_structure_daily (
    trade_date DATE NOT NULL,
    tenor_days INTEGER NOT NULL,
    vix_value DOUBLE,
    source VARCHAR NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (trade_date, tenor_days, ingested_at)
);
