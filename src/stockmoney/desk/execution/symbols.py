"""Symbol spelling between the universe (yfinance style) and the broker.

The universe and the ledger use yfinance spellings ("BRK-B"). Alpaca spells
share classes with a dot ("BRK.B"). Every Alpaca call goes through
``to_broker`` / ``from_broker`` so the rest of the system only ever sees the
universe spelling.

The installed alpaca-py package does not document class-share spelling, so the
mapping is an explicit dict. Confirm each entry against the paper account with
scripts/alpaca_check.py before relying on it.
"""
from __future__ import annotations

TO_BROKER: dict[str, str] = {"BRK-B": "BRK.B"}
FROM_BROKER: dict[str, str] = {v: k for k, v in TO_BROKER.items()}


def to_broker(symbol: str) -> str:
    return TO_BROKER.get(symbol, symbol)


def from_broker(symbol: str) -> str:
    return FROM_BROKER.get(symbol, symbol)
