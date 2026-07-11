CREATE TABLE macro_series_daily (
    series_id VARCHAR NOT NULL,         -- FRED series id, e.g. 'DGS10', 'DTWEXBGS'
    observation_date DATE NOT NULL,
    value DOUBLE,
    vintage_date DATE NOT NULL,         -- revision vintage (ALFRED), not the observation date
    source VARCHAR NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (series_id, observation_date, vintage_date)
);
