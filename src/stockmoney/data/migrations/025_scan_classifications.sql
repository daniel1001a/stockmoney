-- Idempotency ledger for stockmoney.data.scan_classify: records that a raw
-- Reddit post / news article has already been sent through LLM
-- classification, independent of news_articles_raw/social_posts_raw staying
-- append-only. Without this, a cron job re-running over a trailing time
-- window (like the existing OpenClaw fetch_unclassified.py --hours 6) would
-- re-classify (and re-bill) the same items on every run.
--
-- 'irrelevant' items are recorded too, not just successful sentiment/
-- candidate writes -- otherwise an item the model correctly judged
-- off-topic would be re-sent to the API forever.
CREATE TABLE scan_classifications (
    item_id VARCHAR NOT NULL,        -- post_id or article_id
    item_type VARCHAR NOT NULL,      -- 'reddit' | 'news'
    processed_at TIMESTAMPTZ NOT NULL,
    model_version VARCHAR NOT NULL,
    result_kind VARCHAR NOT NULL,    -- 'sentiment' | 'candidate' | 'irrelevant' | 'error'
    symbols VARCHAR,                 -- JSON array of symbols this item produced records for (NULL if none)
    PRIMARY KEY (item_id, item_type)
);
