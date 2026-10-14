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

import math
import re
from dataclasses import dataclass
from datetime import date

TO_BROKER: dict[str, str] = {"BRK-B": "BRK.B"}
FROM_BROKER: dict[str, str] = {v: k for k, v in TO_BROKER.items()}


def to_broker(symbol: str) -> str:
    return TO_BROKER.get(symbol, symbol)


def from_broker(symbol: str) -> str:
    return FROM_BROKER.get(symbol, symbol)


# ---- OCC option symbols ------------------------------------------------------
#
# The OCC symbol is 21 characters: the option root left-justified and padded
# with spaces to 6, expiry YYMMDD, "C" or "P", and the strike times 1000 as 8
# digits ("SPY   261013C00580000"). Alpaca spells the same contract without
# the padding ("SPY261013C00580000"; docs.alpaca.markets options-trading
# examples such as "AAPL240119C00100000"), so occ_symbol returns that form by
# default. The last 15 characters always have the fixed layout, which makes
# parsing unambiguous even for roots that end in a digit (adjusted contracts).

_OCC_TAIL = re.compile(r"(\d{2})(\d{2})(\d{2})([CP])(\d{8})")
_OCC_ROOT = re.compile(r"[A-Z][A-Z0-9]{0,5}")
OCC_LEN = 21
_MAX_STRIKE_MILLI = 99_999_999


@dataclass(frozen=True)
class OccSymbol:
    root: str  # the option root; equals the underlying for standard contracts
    expiry: date
    right: str  # "C" or "P"
    strike: float


def occ_symbol(underlying: str, expiry: date, right: str, strike: float, *, padded: bool = False) -> str:
    """Build the OCC symbol. ``padded=True`` returns the 21-character form;
    the default is the unpadded form the broker uses. Raises ValueError for a
    root, right or strike the format cannot represent exactly."""
    root = (underlying or "").strip().upper()
    if not _OCC_ROOT.fullmatch(root):
        raise ValueError(f"option root must be 1-6 letters/digits starting with a letter, got {underlying!r}")
    r = (right or "").strip().upper()
    r = {"CALL": "C", "PUT": "P"}.get(r, r)
    if r not in ("C", "P"):
        raise ValueError(f"right must be 'C' or 'P', got {right!r}")
    if not isinstance(expiry, date) or not (2000 <= expiry.year <= 2099):
        raise ValueError(f"expiry must be a date in 2000-2099, got {expiry!r}")
    try:
        k = float(strike)
    except (TypeError, ValueError):
        raise ValueError(f"strike must be a number, got {strike!r}") from None
    if not math.isfinite(k):
        raise ValueError(f"strike must be finite, got {strike!r}")
    milli = round(k * 1000)
    if not (0 < milli <= _MAX_STRIKE_MILLI) or abs(k * 1000 - milli) > 1e-6:
        raise ValueError(f"strike {strike!r} is not a positive price with at most 3 decimals below 100000")
    head = root.ljust(6) if padded else root
    return f"{head}{expiry:%y%m%d}{r}{milli:08d}"


def parse_occ(occ: str) -> OccSymbol:
    """Parse either spelling (padded 21-character or unpadded). Raises
    ValueError for anything that is not a well-formed OCC symbol."""
    if not isinstance(occ, str) or len(occ) < 16 or len(occ) > OCC_LEN:
        raise ValueError(f"not an OCC option symbol: {occ!r}")
    tail = _OCC_TAIL.fullmatch(occ[-15:])
    head = occ[:-15]
    root = head.rstrip(" ")
    if tail is None or not _OCC_ROOT.fullmatch(root) or (len(head) != len(root) and len(head) != 6):
        raise ValueError(f"not an OCC option symbol: {occ!r}")
    yy, mm, dd, right, milli = tail.groups()
    try:
        expiry = date(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        raise ValueError(f"OCC symbol {occ!r} has an invalid expiry") from None
    if int(milli) <= 0:
        raise ValueError(f"OCC symbol {occ!r} has a zero strike")
    return OccSymbol(root, expiry, right, int(milli) / 1000.0)


def is_occ_symbol(symbol: str) -> bool:
    """True when ``symbol`` parses as an OCC option symbol (either spelling).
    Equity tickers ("AAPL", "BRK-B", "BRK.B") and crypto pairs never do."""
    try:
        parse_occ(symbol)
        return True
    except ValueError:
        return False
