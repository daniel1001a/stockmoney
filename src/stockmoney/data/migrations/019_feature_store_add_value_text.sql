-- Forward-only migration: add a nullable text column so categorical features
-- (e.g. regime labels: trend_up / trend_down / range-bound; CLAUDE.md sections 4-5)
-- can be stored without lossy numeric encoding. Numeric features keep using
-- feature_value; categorical features set feature_value_text (feature_value may be NULL).
ALTER TABLE feature_store ADD COLUMN IF NOT EXISTS feature_value_text VARCHAR;
