-- Trader registry for the Trader League Arena
-- (~/.claude/plans/trader-council-rearchitecture.md section 2, worker-1 arena).
-- Each row is one simulated trader with a distinct *core philosophy* and a
-- binding to a prediction *engine* (ENGINE_REGISTRY key in
-- stockmoney.league.engines). Traders come and go (人數流動): a trader is
-- deactivated by setting removed_date + active=false, never hard-deleted, so
-- its historical predictions/review stay attributable.
--
-- This is a discretion/meta-layer concept (CLAUDE.md section 7): the registry
-- and everything keyed off trader_id (trader_predictions / trader_review_log /
-- trader_method_*) is NEVER read by any training path -- it must not silently
-- re-enter the clean, backtestable base models.
CREATE TABLE traders (
    trader_id VARCHAR NOT NULL,     -- stable key, e.g. 'chartist' | 'analyst'
    name VARCHAR NOT NULL,          -- display name (技術派 / 消息派)
    philosophy VARCHAR NOT NULL,    -- one-line core trading philosophy
    engine_key VARCHAR NOT NULL,    -- binds to stockmoney.league.engines.ENGINE_REGISTRY
    active BOOLEAN NOT NULL DEFAULT true,
    added_date DATE NOT NULL,
    removed_date DATE,              -- non-NULL once retired; active goes false with it
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (trader_id)
);

-- Seed the two v1 traders (rearchitecture doc: Chartist + Analyst first,
-- NN/contrarian/momentum later). Idempotent because migrations apply once.
INSERT INTO traders (trader_id, name, philosophy, engine_key, active, added_date, created_at) VALUES
    ('chartist', 'Chartist (技術派)',
     'Regime-aware technical timing: read the market state, then bet the direction module A/B''s probabilities favour when the EV gate is open.',
     'chartist', true, DATE '2026-07-11', now()),
    ('analyst', 'Analyst (消息派)',
     'News-cascade reasoning: trace a concrete transmission chain from a catalyst to the symbol and bet on what the market has not yet fully priced in.',
     'analyst', true, DATE '2026-07-11', now());
