CREATE TABLE alt_social_hourly (
    symbol VARCHAR NOT NULL,
    platform VARCHAR NOT NULL,          -- 'reddit' ...
    hour_bucket TIMESTAMPTZ NOT NULL,
    post_count BIGINT,
    sentiment_score DOUBLE,
    sentiment_accel DOUBLE,             -- second derivative of sentiment
    source VARCHAR NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (symbol, platform, hour_bucket, ingested_at)
);
