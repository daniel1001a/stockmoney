"""Authority rule for Muse input: it can only REDUCE risk.

``active_flags`` turns stored Muse messages into three restrictions:
- ``blocked``: candidate (not held) tickers that may not be entered
- ``no_add``: held tickers that may not be added to
- ``notify``: lines to push to the owner

There is deliberately no output here that opens a position, raises a size or
changes a parameter, and no code path from a message to an order. IDEA,
SOCIAL_DAILY, CALLS and GROK_DAILY produce no flags (incubator / feature-store
data; GROK_DAILY stays store-only until Grok has its own token).

Timing uses ``received_at`` (set by our inbox), never Muse's own timestamps, so
a message can neither back-date nor extend its own effect. Whether a ticker is
held comes from the caller, never from Muse.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Collection

from pydantic import TypeAdapter, ValidationError

from stockmoney.desk.bridge.inbox import _clean, ensure_schema
from stockmoney.desk.bridge.schema import Alert, Inbound, ResearchReply, Severity

FLAG_DAYS = 3
NOTIFY_HOURS = 24
# Rows older than the longest effect window (plus slack for clock skew) can
# never produce a flag; the SQL lower bound keeps the scan from growing forever.
_SCAN_MARGIN = timedelta(days=1)
_INBOUND = TypeAdapter(Inbound)
# Noise, not signal: stray senders and re-polled duplicates are logged but never pushed.
_QUIET_REJECTS = ("sender_not_allowed", "duplicate_msg_id", "reply_prefix duplicate_msg_id")


@dataclass(frozen=True)
class BridgeFlags:
    blocked: dict[str, str] = field(default_factory=dict)
    no_add: dict[str, str] = field(default_factory=dict)
    notify: list[str] = field(default_factory=list)


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _parse_ts(s: str) -> datetime:
    return _utc(datetime.fromisoformat(s))


def _add(d: dict[str, str], ticker: str, reason: str) -> None:
    d[ticker] = f"{d[ticker]} | {reason}"[:600] if ticker in d else reason


def active_flags(con, now: datetime, held: Collection[str] = ()) -> BridgeFlags:
    """Flags in force at ``now``. ``held`` is the set of tickers the desk
    currently holds (from the ledger); every other ticker is a candidate."""
    ensure_schema(con)
    now = _utc(now)
    held_set = {t.upper() for t in held}
    flag_cutoff = now - timedelta(days=FLAG_DAYS)
    notify_cutoff = now - timedelta(hours=NOTIFY_HOURS)
    blocked: dict[str, str] = {}
    no_add: dict[str, str] = {}
    notify: list[str] = []

    scan_from = now - max(timedelta(days=FLAG_DAYS), timedelta(hours=NOTIFY_HOURS)) - _SCAN_MARGIN
    rows = con.execute(
        "SELECT type, received_at, parsed_json FROM bridge_messages "
        "WHERE type IN ('ALERT','RESEARCH_REPLY') AND received_at >= ? ORDER BY received_at",
        (scan_from.isoformat(timespec="seconds"),),
    ).fetchall()
    for typ, received_at, parsed in rows:
        try:
            when = _parse_ts(received_at)
            msg = _INBOUND.validate_json(parsed)
        except (ValueError, ValidationError):
            continue  # unreadable row: ignore, never guess
        if when > now:  # cannot happen via poll; do not let a bad clock extend a flag
            continue
        ticker = msg.ticker.upper()
        is_held = ticker in held_set
        if isinstance(msg, Alert):
            headline = _clean(msg.headline, 140)
            if when >= flag_cutoff:
                if not is_held:
                    _add(blocked, ticker, f"Muse ALERT {msg.severity.value}: {headline}")
                elif msg.severity is Severity.critical:
                    _add(no_add, ticker, f"Muse critical ALERT: {headline}")
            if when >= notify_cutoff and msg.severity in (Severity.critical, Severity.elevated):
                role = "held" if is_held else "candidate"
                notify.append(f"Muse ALERT {msg.severity.value} {ticker} ({role}): {headline}")
        elif isinstance(msg, ResearchReply):
            if when >= flag_cutoff and msg.risk_flags and not is_held:
                flags = _clean("; ".join(msg.risk_flags), 200)
                _add(blocked, ticker, f"Muse risk flags: {flags}")

    rejects = con.execute(
        "SELECT reason FROM bridge_rejects WHERE received_at >= ?",
        (notify_cutoff.isoformat(timespec="seconds"),),
    ).fetchall()
    counts = Counter(r[0].split(" ")[0] for r in rejects if not r[0].startswith(_QUIET_REJECTS))
    if counts:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        notify.append(f"Muse bridge rejected {sum(counts.values())} message(s) in the last {NOTIFY_HOURS}h ({detail})")
    return BridgeFlags(blocked=blocked, no_add=no_add, notify=notify)
