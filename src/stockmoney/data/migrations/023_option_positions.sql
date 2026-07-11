-- CLAUDE.md section 10.1: the options risk track needs a ledger of what the
-- human actually holds -- entry terms it can later re-evaluate stop/take-profit
-- triggers against. Unlike the raw ingestion tables this is user-authored state
-- (not an external feed subject to look-ahead risk), so it is an ordinary
-- mutable table: a position is opened once, then closed in place when the
-- human exits. It is never read by any model training path (section 7's
-- discretion/meta-layer separation) -- only by the risk-check layer.
CREATE TABLE option_positions (
    position_id VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    option_right VARCHAR NOT NULL,        -- 'call' | 'put'
    side VARCHAR NOT NULL,                -- 'long' (buyer) | 'short' (seller)
    strike DOUBLE NOT NULL,
    expiry_date DATE NOT NULL,
    entry_date DATE NOT NULL,
    entry_underlying_price DOUBLE NOT NULL,
    entry_premium DOUBLE NOT NULL,        -- price paid (long) or collected (short), per share
    entry_iv DOUBLE,                      -- underlying IV proxy at entry, for the Z-value price stop
    regime_at_entry INTEGER,              -- nullable: filled in once production regime serving exists
    thesis_note VARCHAR,                  -- free-text reason for the trade (discretion layer)
    status VARCHAR NOT NULL DEFAULT 'open', -- 'open' | 'closed'
    closed_at TIMESTAMPTZ,
    closed_reason VARCHAR,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (position_id)
);
