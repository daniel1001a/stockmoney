-- Raw TypeSafe Jev judgments for the scan classification pass
-- (stockmoney.data.jev_classify). Append-only: one row per item per Jev call,
-- storing the probabilities/confidences BEFORE any routing threshold is
-- applied, so thresholds can be re-calibrated on shadow-run data without
-- re-calling the model. `route` records what the router did with the item:
--   direct_sentiment | direct_irrelevant | escalated | jev_error
-- `reason` is the escalation/error cause (NULL for direct routes).
CREATE TABLE IF NOT EXISTS jev_judgments (
    item_id        VARCHAR NOT NULL,
    item_type      VARCHAR NOT NULL,   -- 'reddit' | 'news'
    judged_at      TIMESTAMP NOT NULL,
    model          VARCHAR,            -- response.model as returned by the API
    relevant_p     DOUBLE,
    candidate_p    DOUBLE,
    symbols_json   VARCHAR,            -- JSON: {SYMBOL: {discusses_p, sentiment, confidence}}
    route          VARCHAR NOT NULL,
    reason         VARCHAR,
    input_tokens   BIGINT,
    output_tokens  BIGINT
);
