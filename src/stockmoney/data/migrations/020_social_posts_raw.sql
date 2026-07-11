-- Raw Reddit posts/comments (CLAUDE.md section 3 "動態擴充機制" input source).
-- Append-only ledger: engagement numbers (score/num_comments) change as a
-- post ages, so re-scraping the same post_id records a fresh snapshot rather
-- than overwriting history.
CREATE TABLE social_posts_raw (
    post_id VARCHAR NOT NULL,
    subreddit VARCHAR NOT NULL,
    title VARCHAR,
    body VARCHAR,
    author VARCHAR,
    posted_at TIMESTAMPTZ NOT NULL,
    score INTEGER,
    num_comments INTEGER,
    url VARCHAR,
    source VARCHAR NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (post_id, ingested_at)
);
