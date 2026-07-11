-- Weekend calibration campaign (~/.claude/plans/frolicking-wishing-cook.md):
-- systematic walk-forward sweep of CLAUDE.md section 16's "not yet
-- optimized" parameters (regime count, band_k, EV gate window/quantile,
-- Kelly conservative_coeff), with an explicit held-out confirmation window
-- so results can't be "improved" by simply trying enough combinations on the
-- same history (CLAUDE.md section 12's ban on picking a winner after a
-- full-sample backtest).
--
-- Read-only analysis side-branch, same discipline as attribution_log /
-- feature_candidates: never read by any training path, never auto-promoted
-- into production.py / ev_gate.py / kelly.py's defaults. A human reviews
-- calibration_candidates and manually edits the module constant if
-- warranted.
CREATE TABLE calibration_runs (
    run_id VARCHAR NOT NULL,
    campaign_id VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    sector VARCHAR NOT NULL,
    phase VARCHAR NOT NULL,             -- 'search' | 'confirm'
    param_family VARCHAR NOT NULL,      -- 'regime' (Tier 1 sweep axis this run belongs to)
    params VARCHAR NOT NULL,            -- JSON {method, n_regimes, band_k, seed}
    n_folds INTEGER NOT NULL,
    initial_frac DOUBLE NOT NULL,
    embargo INTEGER NOT NULL,
    seed INTEGER NOT NULL,
    overall_accuracy DOUBLE,
    overall_brier DOUBLE,
    overall_sharpe DOUBLE,
    baseline_accuracy DOUBLE,           -- majority-class baseline, from metrics.report_by_regime
    per_regime_json VARCHAR,            -- JSON list of {regime,n,accuracy,brier,sharpe} -- never merged into one number
    vs_baseline_json VARCHAR,           -- JSON list of {regime,n,mean_diff,ci_lo,ci_hi,improved,worsened} vs the fixed production baseline
    n_obs INTEGER,
    criteria_passes BOOLEAN,            -- >=1 regime significantly improved AND 0 regimes significantly worsened
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (run_id)
);

-- Only configs that survived the held-out confirmation check land here for
-- human review ('proposed' if confirm also passed, 'rejected' if it didn't --
-- an auto-rejection by the holdout test itself needs no human review queue
-- time). Mirrors feature_candidates: propose, never auto-promote.
CREATE TABLE calibration_candidates (
    candidate_id VARCHAR NOT NULL,
    campaign_id VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    params VARCHAR NOT NULL,            -- JSON {method, n_regimes, band_k, seed}
    search_verdict VARCHAR NOT NULL,    -- 'passed' (only passing search rows are ever shortlisted into confirmation)
    confirm_verdict VARCHAR NOT NULL,   -- 'passed' | 'failed', against the untouched holdout tail
    ev_kelly_json VARCHAR,              -- informational post-hoc EV-gate/Kelly grid over this config's own outcomes (never a second statistical gate)
    status VARCHAR NOT NULL,            -- 'proposed' | 'accepted' | 'rejected'
    proposed_date DATE NOT NULL,
    reviewed_by VARCHAR,
    reviewed_date DATE,
    notes VARCHAR,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (candidate_id)
);
