-- Virtual options-trading accounts for the Trader League Arena. The user asked
-- for the league to feel like each trader is running a *real short-dated
-- options account* in a contest: a starting bankroll, position-size limits, a
-- ledger of trades with entry/exit/time/P&L, and a live equity curve you can
-- click into (CLAUDE.md's league concept, section 0/7 -- still discretion/meta
-- layer, NEVER read by any training path).
--
-- These are simulated bookkeeping tables written by the league engine
-- (offline, nightly), not an external feed, so they are ordinary mutable
-- tables like option_positions -- not append-only ingestion tables. The hard
-- product boundary still holds: this system never places a real order. A
-- "trade" here is a simulated fill used only to score the contest.
CREATE TABLE trader_portfolios (
    trader_id VARCHAR NOT NULL,
    starting_capital DOUBLE NOT NULL,     -- contest bankroll at inception
    cash DOUBLE NOT NULL,                 -- uninvested cash right now
    max_position_pct DOUBLE NOT NULL,     -- contest rule: max % of capital per position
    instrument_scope VARCHAR NOT NULL,    -- contest rule, e.g. 'short_dated_calls_puts'
    inception_date DATE NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (trader_id)
);

-- One row per simulated options trade. Open trades have status='open' and NULL
-- exit_* / realized_pnl; they ARE the trader's current holdings (the frontend
-- derives "current positions" from status='open'). realized_pnl is in dollars
-- (per-share P&L x 100 x contracts), the natural contest scoring unit.
CREATE TABLE trader_trades (
    trade_id VARCHAR NOT NULL,
    trader_id VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    option_right VARCHAR NOT NULL,        -- 'call' | 'put'
    side VARCHAR NOT NULL,                -- 'long' | 'short'
    strike DOUBLE NOT NULL,
    expiry_date DATE NOT NULL,
    contracts INTEGER NOT NULL,
    entry_at TIMESTAMPTZ NOT NULL,
    entry_underlying DOUBLE NOT NULL,
    entry_premium DOUBLE NOT NULL,        -- per share
    exit_at TIMESTAMPTZ,                  -- NULL while open
    exit_underlying DOUBLE,
    exit_premium DOUBLE,                  -- per share, at exit
    realized_pnl DOUBLE,                  -- dollars; NULL while open
    status VARCHAR NOT NULL DEFAULT 'open', -- 'open' | 'closed'
    thesis VARCHAR,                       -- why the trade was put on
    exit_reason VARCHAR,                  -- why it was closed (target/stop/expiry/logic)
    linked_prediction_id VARCHAR,         -- optional trader_predictions.prediction_id this expressed
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (trade_id)
);
