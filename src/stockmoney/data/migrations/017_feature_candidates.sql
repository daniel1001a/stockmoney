-- Staging table for the "new feature candidate list" side-channel
-- (CLAUDE.md section 1 architecture side-branch + section 11).
-- Candidates require human review before promotion into the feature store.
CREATE TABLE feature_candidates (
    candidate_id VARCHAR NOT NULL,
    proposed_date DATE NOT NULL,
    proposed_feature_name VARCHAR NOT NULL,
    hypothesis VARCHAR NOT NULL,        -- independently verifiable feature hypothesis
    source_attribution_date DATE,       -- which attribution_log 'wrong_signal_existed' case it came from
    status VARCHAR NOT NULL,            -- 'proposed' | 'under_review' | 'accepted' | 'rejected'
    reviewed_by VARCHAR,
    reviewed_date DATE,
    notes VARCHAR,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (candidate_id)
);
