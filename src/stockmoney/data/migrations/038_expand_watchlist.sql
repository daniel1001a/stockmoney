-- Expand the watchlist beyond the original 11 core names so the daily board has
-- more genuine directional shots (the owner's feedback: too few "精選" when the
-- handful of mega-caps all cluster into 盤整 on a given day). All additions are
-- liquid, options-active US names, grouped into the same feature-sectors the
-- xsec_dispersion groups define (data/features/dispersion.py SECTOR_MEMBERS) so
-- production.predict_latest can actually score them -- a symbol whose sector has
-- no dispersion feature would silently return no prediction.
--
-- Two new sectors (financials, energy) broaden coverage beyond tech/semis so the
-- board isn't all one macro bet. SOXX/QQQ are the sector-ETF expressions the
-- owner asked for; they map to their underlying single-name dispersion group via
-- watchlist._FEATURE_SECTOR_ALIASES (same pattern as SOXL/SOXS -> semiconductor).
INSERT INTO watchlist_members (symbol, tier, sector, added_date, added_by) VALUES
    -- semiconductor single names
    ('MU',   'core',      'semiconductor',     CURRENT_DATE, 'manual'),
    ('QCOM', 'core',      'semiconductor',     CURRENT_DATE, 'manual'),
    ('MRVL', 'core',      'semiconductor',     CURRENT_DATE, 'manual'),
    ('INTC', 'secondary', 'semiconductor',     CURRENT_DATE, 'manual'),
    -- big-tech / high-beta growth single names
    ('TSLA', 'core',      'big_tech',          CURRENT_DATE, 'manual'),
    ('NFLX', 'core',      'big_tech',          CURRENT_DATE, 'manual'),
    ('ORCL', 'secondary', 'big_tech',          CURRENT_DATE, 'manual'),
    ('CRM',  'secondary', 'big_tech',          CURRENT_DATE, 'manual'),
    ('PLTR', 'core',      'big_tech',          CURRENT_DATE, 'manual'),
    -- financials
    ('JPM',  'core',      'financials',        CURRENT_DATE, 'manual'),
    ('BAC',  'secondary', 'financials',        CURRENT_DATE, 'manual'),
    ('GS',   'core',      'financials',        CURRENT_DATE, 'manual'),
    ('MS',   'secondary', 'financials',        CURRENT_DATE, 'manual'),
    ('WFC',  'secondary', 'financials',        CURRENT_DATE, 'manual'),
    -- energy
    ('XOM',  'core',      'energy',            CURRENT_DATE, 'manual'),
    ('CVX',  'secondary', 'energy',            CURRENT_DATE, 'manual'),
    ('COP',  'secondary', 'energy',            CURRENT_DATE, 'manual'),
    ('SLB',  'secondary', 'energy',            CURRENT_DATE, 'manual'),
    -- sector ETFs (the owner's index-opportunity ask); alias to their
    -- single-name dispersion group for feature computation.
    ('SOXX', 'core',      'semiconductor_etf', CURRENT_DATE, 'manual'),
    ('QQQ',  'core',      'big_tech_etf',      CURRENT_DATE, 'manual');
