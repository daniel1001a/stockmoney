-- Trader method evolution (rearchitecture doc section 2 "交易員進化紀律").
-- A trader "learns and updates its method" -- but doing that by chasing
-- yesterday's results is overfitting to noise and violates the whole
-- project's discipline. So evolution is: VERSIONED and FORWARD-ONLY.
--
-- trader_method_versions: the immutable catalogue of each trader's method
-- versions. Every trader_predictions row pins the method_version it was made
-- under. A new version only ever applies going forward; we NEVER re-run an old
-- prediction under a new method (walk-forward rule).
--
-- trader_method_proposals: v1 does NOT auto-mutate. The review layer
-- (stockmoney.league.review) emits a 'proposed' method update when it spots a
-- systematic pattern; a human reviews and only then is a new version minted
-- and marked 'active' (CLAUDE.md section 7 human veto + section 11 candidates
-- are proposals, not auto-promotions). Full auto-evolution is a v2 item and
-- would itself need to pass an out-of-sample significance test.
CREATE TABLE trader_method_versions (
    trader_id VARCHAR NOT NULL,
    method_version VARCHAR NOT NULL,     -- e.g. 'chartist:gmm-logistic-v2', 'analyst:catalyst-v1'
    effective_date DATE NOT NULL,        -- first date predictions may be made under it (forward-only)
    spec VARCHAR NOT NULL,               -- human-readable description of the method at this version
    status VARCHAR NOT NULL,             -- 'active' | 'superseded'
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (trader_id, method_version)
);

CREATE TABLE trader_method_proposals (
    proposal_id VARCHAR NOT NULL,
    trader_id VARCHAR NOT NULL,
    from_version VARCHAR NOT NULL,       -- version the proposal is measured against
    to_version VARCHAR,                  -- filled when a human accepts and mints the new version
    rationale VARCHAR NOT NULL,          -- why the review layer thinks the method should change
    source_review_date DATE,             -- which trader_review_log day surfaced the pattern
    status VARCHAR NOT NULL,             -- 'proposed' | 'accepted' | 'rejected'
    reviewed_by VARCHAR,
    reviewed_date DATE,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (proposal_id)
);

-- Seed each v1 trader's current method version. These pins match what the
-- engines emit: Chartist tracks production.MODEL_VERSION, Analyst tracks
-- catalyst_synthesis.MODEL_VERSION.
INSERT INTO trader_method_versions (trader_id, method_version, effective_date, spec, status, created_at) VALUES
    ('chartist', 'chartist:gmm-logistic-v2', DATE '2026-07-11',
     'GMM regime (3 states, REGIME_COLUMNS only) + per-regime calibrated logistic direction model + EV gate; production.predict_latest.',
     'active', now()),
    ('analyst', 'analyst:catalyst-v1', DATE '2026-07-11',
     'Reads the latest catalyst_signals row for the symbol (Sonnet transmission-chain synthesis) and maps sentiment/novelty/priced_in into a direction + conviction. Single-hop; multi-hop is a foreman follow-up.',
     'active', now());
