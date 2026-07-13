-- Broad-market index OHLCV (SPY), kept DELIBERATELY SEPARATE from ohlcv_daily.
--
-- Wave C (models/vrp.py) needs a forward-realized-volatility series for "the
-- market" to pair against VIX (which prices SPX-wide implied vol, not any one
-- watchlist name) -- see CLAUDE.md's regime-detection realized-vol pattern,
-- applied here to a market-wide volatility risk premium (VRP) target instead
-- of a per-symbol one.
--
-- SPY is intentionally NOT added to watchlist_members or ohlcv_daily: several
-- existing queries read ohlcv_daily with no symbol filter and assume every row
-- is a watchlist name (api/queries.py market_summary's "top movers" scan,
-- positions_with_risk's latest-price lookup) -- landing SPY there would leak
-- a non-tradeable benchmark into the dashboard as if it were a watchlist
-- symbol. A dedicated table with the same shape as ohlcv_daily keeps this
-- Wave C addition at zero blast radius on the existing UI/dashboard.
CREATE TABLE market_index_ohlcv_daily (
    symbol VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    adj_close DOUBLE,
    volume BIGINT,
    source VARCHAR NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (symbol, trade_date, ingested_at)
);
