"""Authority rule for Muse input: it can only REDUCE risk.

``active_flags`` turns stored Muse messages into three restrictions:
- ``blocked``: candidate (not held) tickers that may not be entered
- ``no_add``: held tickers that may not be added to
- ``notify``: lines to push to the owner

Muse downgrade (2026-10-21, decided by the account owner): Muse reads social
feeds only, so on its own it blocks a stock entry only with a critical ALERT.
An info ALERT is logged only; an elevated ALERT or a RESEARCH_REPLY with
risk_flags blocks only through ``veto.check``, which needs a second independent
source in the window. ``legacy_blocked`` keeps what the pre-2026-10-21 rule
(any ALERT or risk flag on a candidate) would have blocked: the options sleeve
still uses it, and premarket logs it for the blocked-entry outcome log
(``record_legacy``) and the ``shadow:combined_muse_strict`` control book.

There is deliberately no output here that opens a position, raises a size or
changes a parameter, and no code path from a message to an order. IDEA,
SOCIAL_DAILY, CALLS, GROK_DAILY and OPTIONS_PLAN produce no flags (incubator / feature-store
data; GROK_DAILY stays store-only until Grok has its own token; an OPTIONS_PLAN's only
effect is the plan the options engine reads from options_plans).

Grok ALERT / RESEARCH_REPLY (2026-10-27, sent from GROK_ADDRESS): the same
risk-reducing rules apply, labelled "Grok" instead of "Muse" so the owner sees
who raised it. A Grok ALERT counts as a cross-source vote only in ``veto``.

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

from stockmoney.desk.bridge.inbox import SOURCE_MUSE, _clean, ensure_schema, has_source_column, source_of
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
    legacy_blocked: dict[str, str] = field(default_factory=dict)


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
    legacy: dict[str, str] = {}
    no_add: dict[str, str] = {}
    notify: list[str] = []

    scan_from = now - max(timedelta(days=FLAG_DAYS), timedelta(hours=NOTIFY_HOURS)) - _SCAN_MARGIN
    src = "source" if has_source_column(con) else "NULL"
    rows = con.execute(
        f"SELECT type, received_at, parsed_json, {src} FROM bridge_messages "
        "WHERE type IN ('ALERT','RESEARCH_REPLY') AND received_at >= ? ORDER BY received_at",
        (scan_from.isoformat(timespec="seconds"),),
    ).fetchall()
    for typ, received_at, parsed, source in rows:
        try:
            when = _parse_ts(received_at)
            msg = _INBOUND.validate_json(parsed)
        except (ValueError, ValidationError):
            continue  # unreadable row: ignore, never guess
        if when > now:  # cannot happen via poll; do not let a bad clock extend a flag
            continue
        ticker = msg.ticker.upper()
        is_held = ticker in held_set
        who = "Muse" if source_of(source, typ) == SOURCE_MUSE else "Grok"
        if isinstance(msg, Alert):
            headline = _clean(msg.headline, 140)
            if when >= flag_cutoff:
                if not is_held:
                    reason = f"{who} ALERT {msg.severity.value}: {headline}"
                    _add(legacy, ticker, reason)
                    if msg.severity is Severity.critical:
                        _add(blocked, ticker, reason)
                elif msg.severity is Severity.critical:
                    _add(no_add, ticker, f"{who} critical ALERT: {headline}")
            if when >= notify_cutoff and msg.severity in (Severity.critical, Severity.elevated):
                role = "held" if is_held else "candidate"
                notify.append(f"{who} ALERT {msg.severity.value} {ticker} ({role}): {headline}")
        elif isinstance(msg, ResearchReply):
            if when >= flag_cutoff and msg.risk_flags and not is_held:
                flags = _clean("; ".join(msg.risk_flags), 200)
                _add(legacy, ticker, f"{who} risk flags: {flags}")

    rejects = con.execute(
        "SELECT reason FROM bridge_rejects WHERE received_at >= ?",
        (notify_cutoff.isoformat(timespec="seconds"),),
    ).fetchall()
    counts = Counter(r[0].split(" ")[0] for r in rejects if not r[0].startswith(_QUIET_REJECTS))
    if counts:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        notify.append(f"Bridge rejected {sum(counts.values())} message(s) in the last {NOTIFY_HOURS}h ({detail})")
    return BridgeFlags(blocked=blocked, no_add=no_add, notify=notify, legacy_blocked=legacy)


_LEGACY_SCHEMA = """CREATE TABLE IF NOT EXISTS muse_legacy_blocks (
  client_order_id TEXT PRIMARY KEY, plan_date TEXT, symbol TEXT, reason TEXT, recorded_at TEXT)"""


def record_legacy(con, client_order_id: str, plan_date: str, symbol: str, reason: str, at: datetime) -> None:
    """Log a buy the pre-2026-10-21 Muse rule would have blocked. Read only by
    the blocked-entry outcome log; it never blocks anything. Does not commit."""
    con.execute(_LEGACY_SCHEMA)
    con.execute("INSERT OR IGNORE INTO muse_legacy_blocks VALUES (?,?,?,?,?)",
                (client_order_id, plan_date, symbol.upper(), reason[:500], _utc(at).isoformat(timespec="seconds")))


def legacy_rows(con) -> list:
    con.execute(_LEGACY_SCHEMA)
    return con.execute("SELECT * FROM muse_legacy_blocks").fetchall()
