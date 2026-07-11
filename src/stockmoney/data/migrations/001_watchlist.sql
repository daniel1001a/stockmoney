CREATE TABLE watchlist_members (
    symbol VARCHAR NOT NULL,
    tier VARCHAR NOT NULL,              -- 'core' | 'secondary'
    sector VARCHAR,
    added_date DATE NOT NULL,
    added_by VARCHAR NOT NULL,          -- 'manual' | 'scanner'
    removed_date DATE,
    notes VARCHAR,
    PRIMARY KEY (symbol, added_date)
);

INSERT INTO watchlist_members (symbol, tier, sector, added_date, added_by) VALUES
    ('NVDA', 'core', 'semiconductor', CURRENT_DATE, 'manual'),
    ('AVGO', 'core', 'semiconductor', CURRENT_DATE, 'manual'),
    ('AMD', 'core', 'semiconductor', CURRENT_DATE, 'manual'),
    ('TSM', 'core', 'semiconductor', CURRENT_DATE, 'manual'),
    ('SOXL', 'core', 'semiconductor_etf', CURRENT_DATE, 'manual'),
    ('SOXS', 'core', 'semiconductor_etf', CURRENT_DATE, 'manual'),
    ('AAPL', 'core', 'big_tech', CURRENT_DATE, 'manual'),
    ('MSFT', 'core', 'big_tech', CURRENT_DATE, 'manual'),
    ('GOOGL', 'core', 'big_tech', CURRENT_DATE, 'manual'),
    ('META', 'core', 'big_tech', CURRENT_DATE, 'manual'),
    ('AMZN', 'core', 'big_tech', CURRENT_DATE, 'manual');
