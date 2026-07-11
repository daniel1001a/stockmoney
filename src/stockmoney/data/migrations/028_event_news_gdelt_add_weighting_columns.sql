-- Forward-only migration: the original event_news_gdelt schema (014) only
-- stores tone_score (AvgTone), with no room for the fields the GDELT sentiment
-- feature (stockmoney.data.features.gdelt_sentiment) needs to compute a
-- mention-weighted daily aggregate and a coverage-quality signal --
-- NumMentions (weighting), GoldsteinScale (a second, structurally different
-- candidate feature), and NumSources/NumArticles (coverage-quality, since
-- GDELT's source coverage/translation quality is known to vary by era --
-- surfacing these lets a later ablation-test breakdown separate "genuinely
-- weak signal" from "this era's data was thin"). All nullable so any other
-- source writing to this table (none exist yet) isn't forced to populate them.
ALTER TABLE event_news_gdelt ADD COLUMN IF NOT EXISTS goldstein_scale DOUBLE;
ALTER TABLE event_news_gdelt ADD COLUMN IF NOT EXISTS num_mentions BIGINT;
ALTER TABLE event_news_gdelt ADD COLUMN IF NOT EXISTS num_sources BIGINT;
ALTER TABLE event_news_gdelt ADD COLUMN IF NOT EXISTS num_articles BIGINT;
