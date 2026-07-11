CREATE TABLE event_calendar (
    event_id VARCHAR NOT NULL,          -- source-provided id, or self-composed hash
    symbol VARCHAR,                     -- NULL = macro event (FOMC/CPI/NFP)
    event_type VARCHAR NOT NULL,        -- 'earnings' | 'fomc' | 'cpi' | 'nfp' ...
    scheduled_at TIMESTAMPTZ NOT NULL,
    status VARCHAR,                     -- 'scheduled' | 'confirmed' | 'released'
    source VARCHAR NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (event_id, ingested_at)
);
