"""Small read-only lookups against `watchlist_members`."""
from __future__ import annotations

import duckdb

# watchlist_members.sector is a display/taxonomy label (e.g. leveraged ETFs
# get their own "semiconductor_etf" tag); feature engineering's xsec_dispersion
# is computed per *feature-group* sector, which for these ETFs is still their
# underlying sector's group ("semiconductor"), not a separate ETF group -- see
# backtest_semiconductor.py/backtest_ev_gate.py, both of which call
# build_feature_matrix(target_symbol="SOXL", sector="semiconductor") literally.
# Without this alias, sector_for_symbol("SOXL") would return "semiconductor_etf",
# a sector with no computed xsec_dispersion feature, and production.predict_latest
# would silently return None for a symbol that's actually fully supported.
_FEATURE_SECTOR_ALIASES = {
    "semiconductor_etf": "semiconductor",
}


def sector_for_symbol(conn: duckdb.DuckDBPyConnection, symbol: str) -> str | None:
    """The feature-engineering sector group for `symbol` (not necessarily its
    literal watchlist display taxonomy -- see _FEATURE_SECTOR_ALIASES), or
    None if it isn't (or is no longer) an active watchlist member. Feature
    engineering -- and therefore `stockmoney.models.production` -- only has
    data for watchlist symbols."""
    row = conn.execute(
        """
        SELECT sector FROM watchlist_members
        WHERE symbol = ? AND removed_date IS NULL
        ORDER BY added_date DESC LIMIT 1
        """,
        [symbol.upper()],
    ).fetchone()
    if row is None:
        return None
    sector = row[0]
    return _FEATURE_SECTOR_ALIASES.get(sector, sector)
