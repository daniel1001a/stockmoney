"""Ledger vs broker reconciliation. The broker is the source of truth; any
mismatch means the orchestrator must halt new entries and notify a human.
Exits and protective stops keep running for every symbol that reconciles.
Option positions belong to the options sleeve, which reconciles its own book
(execution.options_risk), and are ignored here."""
from __future__ import annotations

import sqlite3

from stockmoney.desk.execution.isolation import without_options
from stockmoney.desk.execution.ledger import Ledger, is_crypto_symbol, norm
from stockmoney.desk.execution.types import AccountSnapshot, AssetClass

CRYPTO_QTY_TOL = 1e-6
EQUITY_QTY_TOL = 1e-9  # "exact": only absorbs float representation noise


def mismatches(ledger: Ledger | sqlite3.Connection, snapshot: AccountSnapshot) -> dict[str, str]:
    """{normalized symbol: human-readable problem}; empty when the ledger and
    the broker agree on every symbol. Crypto dust below CRYPTO_QTY_TOL is
    ignored on either side, including when only one side holds it."""
    if not isinstance(ledger, Ledger):
        ledger = Ledger(ledger)
    mine = {norm(s): p for s, p in ledger.positions().items()}
    theirs = {norm(p.symbol): p for p in without_options(snapshot).positions if abs(p.qty) > 0}
    out: dict[str, str] = {}
    for key in sorted(set(mine) | set(theirs)):
        led, brk = mine.get(key), theirs.get(key)
        crypto = ((brk is not None and brk.asset_class == AssetClass.CRYPTO)
                  or (led is not None and is_crypto_symbol(led.symbol)))
        tol = CRYPTO_QTY_TOL if crypto else EQUITY_QTY_TOL
        if led is not None and brk is not None:
            if abs(led.qty - brk.qty) > tol:
                out[key] = (f"{brk.symbol}: quantity mismatch, ledger {led.qty:g} ({led.strategy}) "
                            f"vs broker {brk.qty:g}")
        elif brk is not None:
            if abs(brk.qty) > tol:
                out[key] = f"{brk.symbol}: broker holds {brk.qty:g} but the ledger has no such position"
        elif abs(led.qty) > tol:
            out[key] = f"{led.symbol}: ledger holds {led.qty:g} ({led.strategy}) but the broker has no such position"
    return out


def reconcile(ledger: Ledger | sqlite3.Connection, snapshot: AccountSnapshot) -> list[str]:
    """Return human-readable mismatch strings; an empty list means the ledger
    and the broker agree on every symbol."""
    return list(mismatches(ledger, snapshot).values())
