-- Raw financial news articles from free RSS feeds (WSJ, MarketWatch,
-- Investing.com, Seeking Alpha).
CREATE TABLE news_articles_raw (
    article_id VARCHAR NOT NULL,        -- RSS guid, or a hash of the URL if no guid
    source_name VARCHAR NOT NULL,       -- 'wsj' | 'marketwatch' | 'investing' | 'seeking_alpha'
    title VARCHAR,
    summary VARCHAR,
    url VARCHAR,
    published_at TIMESTAMPTZ,
    source VARCHAR NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (article_id, ingested_at)
);
