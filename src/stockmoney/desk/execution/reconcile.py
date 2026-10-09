"""Ledger vs broker reconciliation. The broker is the source of truth; any
mismatch means the orchestrator must halt new entries and notify a human."""
from __future__ import annotations

import sqlite3

from stockmoney.desk.execution.ledger import Ledger, is_crypto_symbol, norm
from stockmoney.desk.execution.types import AccountSnapshot, AssetClass

CRYPTO_QTY_TOL = 1e-6
EQUITY_QTY_TOL = 1e-9  # "exact": only absorbs float representation noise


def reconcile(ledger: Ledger | sqlite3.Connection, snapshot: AccountSnapshot) -> list[str]:
    """Return human-readable mismatch strings; an empty list means the ledger
    and the broker agree on every symbol."""
    if not isinstance(ledger, Ledger):
        ledger = Ledger(ledger)
    mine = {norm(s): p for s, p in ledger.positions().items()}
    theirs = {norm(p.symbol): p for p in snapshot.positions if abs(p.qty) > 0}
    out: list[str] = []
    for key in sorted(set(mine) | set(theirs)):
        led, brk = mine.get(key), theirs.get(key)
        if led is not None and brk is not None:
            crypto = brk.asset_class == AssetClass.CRYPTO or is_crypto_symbol(led.symbol)
            tol = CRYPTO_QTY_TOL if crypto else EQUITY_QTY_TOL
            if abs(led.qty - brk.qty) > tol:
                out.append(
                    f"{brk.symbol}: quantity mismatch, ledger {led.qty:g} ({led.strategy}) vs broker {brk.qty:g}")
        elif brk is not None:
            out.append(f"{brk.symbol}: broker holds {brk.qty:g} but the ledger has no such position")
        else:
            out.append(
                f"{led.symbol}: ledger holds {led.qty:g} ({led.strategy}) but the broker has no such position")
    return out
