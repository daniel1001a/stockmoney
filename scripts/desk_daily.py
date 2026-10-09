"""Nightly paper-trading run for the Trading Desk. Run after 16:30 ET.

    .venv/bin/python scripts/desk_daily.py            # refresh prices + run
    .venv/bin/python scripts/desk_daily.py --no-refresh
"""
from __future__ import annotations

import argparse

from stockmoney.desk import live

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-refresh", action="store_true", help="use cached prices")
    args = ap.parse_args()
    print(live.run_daily(refresh=not args.no_refresh))
