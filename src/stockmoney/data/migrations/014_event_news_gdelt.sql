CREATE TABLE event_news_gdelt (
    gdelt_event_id VARCHAR NOT NULL,
    event_datetime TIMESTAMPTZ NOT NULL,
    symbol VARCHAR,
    event_class VARCHAR,
    geo_lat DOUBLE,
    geo_lon DOUBLE,
    tone_score DOUBLE,
    source VARCHAR NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (gdelt_event_id, ingested_at)
);
