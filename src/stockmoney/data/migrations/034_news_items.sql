-- Unified, human-facing news feed powering the redesigned 消息雷達 (news radar)
-- and the per-ticker "Robinhood-style" news list. This is a READ surface for
-- the discretion layer (CLAUDE.md section 1): nothing in stockmoney.models
-- reads it, it is never blended into a model probability. It is a superset of
-- the older single-purpose feeds -- catalyst_signals (our own transmission
-- reasoning), news_articles_raw (RSS headlines) and event_calendar (scheduled
-- events) -- normalised into one shape so the frontend can list, filter and
-- link every kind of market-relevant news together.
--
-- item_type distinguishes the source class so the UI can badge/filter it:
--   'catalyst'       -- our own not-yet-priced-in transmission thesis
--   'analyst_rating' -- sell-side rating/PT change (bank research)
--   'earnings'       -- earnings report / guidance
--   'macro'          -- rates/CPI/Fed/DXY etc (symbol NULL = market-wide)
--   'headline'       -- general financial headline
--
-- summary is a SHORT (1-2 sentence) synopsis and url is the link out -- we
-- deliberately do NOT store full article bodies (copyright + freshness); the
-- detail view shows our synopsis plus the outbound link, exactly the pattern
-- the user asked for ("剪短 + 附上文章連結").
--
-- available_at is the look-ahead-bias guard (CLAUDE.md section 2): the moment
-- the row became known to the system, not the event's real-world time.
CREATE TABLE news_items (
    item_id VARCHAR NOT NULL,
    symbol VARCHAR,                       -- NULL = market-wide / macro
    item_type VARCHAR NOT NULL,           -- 'catalyst'|'analyst_rating'|'earnings'|'macro'|'headline'
    headline VARCHAR NOT NULL,
    summary VARCHAR,                      -- short synopsis (1-2 sentences)
    url VARCHAR,                          -- outbound link to the full source
    source_name VARCHAR,                  -- 'WSJ' | 'Morgan Stanley' | 'FRED' | 'Reddit' ...
    published_at TIMESTAMPTZ NOT NULL,
    sentiment_score DOUBLE,               -- -1 (bearish) .. 1 (bullish); NULL = neutral/n.a.
    importance DOUBLE,                    -- 0 (minor) .. 1 (major market mover)
    novelty_score DOUBLE,                 -- 0 (well-known) .. 1 (fresh/surprising)
    priced_in_estimate DOUBLE,            -- 0 (not priced) .. 1 (already priced in)
    transmission_chain VARCHAR,           -- for item_type='catalyst': catalyst -> reasoning -> symbol
    source_refs VARCHAR,                  -- JSON array of underlying raw ids
    available_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (item_id)
);
