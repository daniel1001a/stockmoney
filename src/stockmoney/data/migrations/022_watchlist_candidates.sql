-- CLAUDE.md section 3 "動態擴充機制": a lightweight scanner proposes candidate
-- tickers/themes; a human keeps final veto power. This table is the proposal
-- queue -- it is never auto-promoted into watchlist_members by any code path.
CREATE TABLE watchlist_candidates (
    candidate_id VARCHAR NOT NULL,
    proposed_date DATE NOT NULL,
    symbol VARCHAR,                     -- filled if a concrete ticker; NULL for a pure theme
    theme VARCHAR,                      -- e.g. 'thermal_management' | 'memory_cycle' | 'space'
    rationale VARCHAR NOT NULL,         -- evidence-based justification, not a vague feeling
    evidence_count INTEGER,
    source_refs VARCHAR,                -- JSON array of post_id/article_id for human verification
    status VARCHAR NOT NULL,            -- 'proposed' | 'accepted' | 'rejected'
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (candidate_id)
);
