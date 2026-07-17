-- 039: expand the trader league from 2 to 5 philosophies.
--
-- The rearchitecture doc (030's seed comment) always intended more factions
-- after the Chartist + Analyst v1 pair. Each new trader binds to an engine in
-- stockmoney.league.engines.ENGINE_REGISTRY by engine_key and pins an active
-- method_version the engines emit verbatim. All three read already-computed
-- features as-of the league day (feature_store / alt_social_hourly) with a
-- look-ahead guard -- no paid API, no schema change, same graded ruler as the
-- existing two. Idempotent: migrations apply exactly once.

INSERT INTO traders (trader_id, name, philosophy, engine_key, active, added_date, created_at) VALUES
    ('reversion', 'Reversion (反轉派)',
     'Mean-reversion: fade RSI/price extremes -- bet the crowd overshot and price snaps back toward its band.',
     'reversion', true, DATE '2026-07-17', now()),
    ('flow', 'Flow (資金流派)',
     'Positioning/flow: read dealer gamma (GEX), 25-delta skew and put/call to infer where option-hedging pressure pushes price.',
     'flow', true, DATE '2026-07-17', now()),
    ('sentiment', 'Sentiment (情緒派)',
     'Sentiment heat: ride the acceleration of social + GDELT news tone -- bet on attention momentum the tape has not caught up to.',
     'sentiment', true, DATE '2026-07-17', now());

INSERT INTO trader_method_versions (trader_id, method_version, effective_date, spec, status, created_at) VALUES
    ('reversion', 'reversion:rsi-meanrev-v1', DATE '2026-07-17',
     'Reads rsi_14 as-of the league day from feature_store; RSI>=70 -> down (fade the rip), <=30 -> up (fade the flush), else range; conviction scales with distance from 50.',
     'active', now()),
    ('flow', 'flow:positioning-v1', DATE '2026-07-17',
     'Reads gex_estimate / skew_25delta_chg_1d / put_call_ratio as-of the league day from feature_store; combines them into a signed positioning-pressure score -> direction + conviction.',
     'active', now()),
    ('sentiment', 'sentiment:heat-accel-v1', DATE '2026-07-17',
     'Reads social sentiment acceleration (alt_social_hourly.sentiment_accel) + GDELT tone (gdelt_avgtone_1d) as-of the league day; net accelerating-positive -> up, accelerating-negative -> down, else range.',
     'active', now());
