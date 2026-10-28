"""Pre-trade risk engine and book for the intraday options sleeve
(docs/desk/OPTIONS_PLAN.md section 3). Safety-critical, same rules as
execution.risk: a bug here is worse than no feature.

Design rules:
- Exits (sell to close, up to the contracts held at the broker) are allowed
  regardless of kill switch, halt, entry window, budget or liquidity. The only
  checks on a sell are: it is a well-formed option intent, it does not sell
  more than is held net of other working sells (no sell to open), and a limit
  price is not a fat-finger far below the market. A market sell is always
  available.
- Entries (buy to open) must pass every rule, and are resized down in whole
  contracts (never silently: the reason says what was cut) when a premium
  cap binds.
- The sleeve has its own book in options_* tables. It never touches the stock
  ledger and is not part of the stock system equity. Sleeve equity is
  sleeve_budget + realized + unrealized P&L of this book.
- Anything that cannot be measured (missing quote, NaN, missing open
  interest, an unmarked open position, a book that disagrees with the broker)
  blocks entries.

Rules (all dollar limits are fractions of the sleeve budget, so they scale
when the owner raises sleeve_pct):
  sleeve budget          capital_budget * sleeve_pct (default 50,000 * 10%)
  per-trade premium      max_trade_pct of the budget (20%)
  open premium at risk   at most the budget, including working buys
  concurrent positions   max_positions (3), one entry per contract
  daily loss stop        realized today + unrealized <= -daily_loss_pct (20%)
  drawdown halt          sleeve equity dd_halt_pct (35%) below its peak:
                         latched until a human calls rearm()
  liquidity              spread/mid, min mid, min open interest, min volume,
                         quote age
  entry window           entry_start..entry_end ET, never at/after flat_by
  kill switch            data/desk/HALT (shared with the stock book), the
                         stock setting halt_new_entries=1, or the setting
                         options_halt_new_entries=1
  Muse                   ALERT / risk flags on the underlying block entries
  price band             a buy's price within price_band of mid
"""
from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from math import floor
from pathlib import Path
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from stockmoney.desk import store
from stockmoney.desk.execution.isolation import is_option
from stockmoney.desk.execution.symbols import parse_occ
from stockmoney.desk.execution.types import (
    OPTION_MULTIPLIER, TIF, AccountSnapshot, AssetClass, BrokerOrder, OptionContract, OrderIntent, OrderStatus,
    OrderType, RiskDecision, Side,
)

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
QTY_EPS = 1e-9
HALT_KEY = "halted"            # options_book_state: reason of a latched drawdown halt ('' = not halted)
PEAK_KEY = "peak_equity"       # options_book_state: persisted sleeve equity peak
PEAK_BUDGET_KEY = "peak_budget"  # the sleeve budget the stored peak was measured against
OPTIONS_HALT_SETTING = "options_halt_new_entries"
_LIVE = (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED, OrderStatus.PENDING, OrderStatus.PENDING_CANCEL)

OPTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS options_book_positions (
  occ TEXT PRIMARY KEY, underlying TEXT NOT NULL, strategy TEXT NOT NULL, qty REAL NOT NULL,
  avg_price REAL NOT NULL, multiplier INTEGER NOT NULL DEFAULT 100, opened_at TEXT);
CREATE TABLE IF NOT EXISTS options_book_fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT, client_order_id TEXT UNIQUE, occ TEXT NOT NULL, underlying TEXT NOT NULL,
  strategy TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL, price REAL NOT NULL,
  multiplier INTEGER NOT NULL DEFAULT 100, fee REAL NOT NULL DEFAULT 0, filled_at TEXT NOT NULL,
  realized_pnl REAL NOT NULL DEFAULT 0, recorded_at TEXT);
CREATE INDEX IF NOT EXISTS options_book_fills_filled_at ON options_book_fills(filled_at);
CREATE TABLE IF NOT EXISTS options_book_state (key TEXT PRIMARY KEY, value TEXT);
"""


def _num(x) -> bool:
    try:
        return x is not None and not isinstance(x, bool) and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _utc_iso(t: datetime | str | None) -> str:
    """Normalise a fill time to UTC ISO seconds, the one format stored (so
    string comparison is time comparison). Naive datetimes are UTC."""
    if t is None:
        t = datetime.now(timezone.utc)
    if isinstance(t, str):
        t = datetime.fromisoformat(t.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def et_day_bounds(day: date) -> tuple[str, str]:
    """[start, end) of an ET calendar day as stored-format UTC strings."""
    start = datetime.combine(day, time(0, 0), tzinfo=ET)
    return _utc_iso(start), _utc_iso(start + timedelta(days=1))


class OptionsBookError(ValueError):
    pass


@dataclass(frozen=True)
class OptionPosition:
    occ: str
    underlying: str
    strategy: str
    qty: float
    avg_price: float  # per share
    multiplier: int
    opened_at: str | None

    @property
    def cost(self) -> float:
        return self.qty * self.avg_price * self.multiplier


class OptionsBook:
    """Attribution book of the options sleeve (options_* tables, created
    idempotently). Long-only, one strategy per contract, idempotent on
    client_order_id exactly like the stock Ledger. The broker stays the
    source of truth; options_mismatches compares the two."""

    def __init__(self, con: sqlite3.Connection):
        self.con = con
        con.executescript(OPTIONS_SCHEMA)

    def positions(self) -> dict[str, OptionPosition]:
        rows = self.con.execute("SELECT occ, underlying, strategy, qty, avg_price, multiplier, opened_at "
                                "FROM options_book_positions ORDER BY occ").fetchall()
        return {r[0]: OptionPosition(*tuple(r)) for r in rows}

    def position(self, occ: str) -> OptionPosition | None:
        return self.positions().get(occ)

    def booked(self, client_order_id: str) -> tuple[float, float]:
        """(qty, qty * price) already booked for one broker order (plain row
        plus every ``<id>#<cumulative qty>`` partial-fill row)."""
        r = self.con.execute(
            "SELECT COALESCE(SUM(qty),0), COALESCE(SUM(qty*price),0) FROM options_book_fills "
            "WHERE client_order_id=? OR substr(client_order_id, 1, ?)=?",
            (client_order_id, len(client_order_id) + 1, client_order_id + "#")).fetchone()
        return float(r[0]), float(r[1])

    def apply_fill(self, *, client_order_id: str, occ: str, strategy: str, side: Side, qty: float, price: float,
                   filled_at: datetime | str | None = None, fee: float = 0.0, cumulative: bool = False,
                   multiplier: int = OPTION_MULTIPLIER) -> bool:
        """Book one fill. Returns False (no-op) when already booked. Raises
        OptionsBookError on any rule violation without writing anything.
        ``cumulative=True``: qty/price are the order's cumulative filled qty
        and average price (as the broker reports partial fills); only the
        part not booked yet is added. ``fee`` (USD, >= 0) reduces realized
        P&L of the row it is booked with."""
        if not client_order_id:
            raise OptionsBookError("client_order_id is required")
        try:
            parts = parse_occ(occ)
        except ValueError as e:
            raise OptionsBookError(str(e)) from None
        if not (_num(qty) and qty > 0 and abs(qty - round(qty)) <= QTY_EPS):
            raise OptionsBookError(f"fill qty must be a whole number of contracts > 0, got {qty!r}")
        if not (_num(price) and price > 0):
            raise OptionsBookError(f"fill price must be positive and finite, got {price!r}")
        if not (_num(fee) and fee >= 0):
            raise OptionsBookError(f"fee must be >= 0 and finite, got {fee!r}")
        if not (isinstance(multiplier, int) and multiplier > 0):
            raise OptionsBookError(f"multiplier must be a positive int, got {multiplier!r}")
        qty, price, fee = float(round(qty)), float(price), float(fee)
        if cumulative:
            done_qty, done_cost = self.booked(client_order_id)
            delta = qty - done_qty
            if delta <= QTY_EPS:
                return False
            if done_qty > QTY_EPS:
                px = (qty * price - done_cost) / delta
                price = px if math.isfinite(px) and px > 0 else price
                client_order_id = f"{client_order_id}#{qty!r}"
            qty = delta
        if self.con.execute("SELECT 1 FROM options_book_fills WHERE client_order_id=?", (client_order_id,)).fetchone():
            return False
        pos = self.position(occ)
        at = _utc_iso(filled_at)
        # every rule is checked BEFORE the transaction: the connection is shared with the
        # options engine, and an exception inside `with self.con:` would roll back the
        # caller's own uncommitted writes too
        if side == Side.BUY:
            if pos is None and not strategy:
                raise OptionsBookError(f"opening {occ} needs a strategy")
            if pos is not None and strategy and pos.strategy != strategy:
                raise OptionsBookError(f"{occ} is held by '{pos.strategy}'; cannot open it under '{strategy}'")
            if pos is not None and pos.multiplier != multiplier:
                raise OptionsBookError(f"{occ} multiplier {multiplier} differs from booked {pos.multiplier}")
        elif side == Side.SELL:
            if pos is None:
                raise OptionsBookError(f"sell of {occ} but the book holds none (no sell to open)")
            if qty > pos.qty + QTY_EPS:
                raise OptionsBookError(f"sell {qty:g} of {occ} exceeds held {pos.qty:g} (no sell to open)")
        else:
            raise OptionsBookError(f"unknown side {side!r}")
        with self.con:
            if side == Side.BUY:
                realized = -fee
                if pos is None:
                    self.con.execute(
                        "INSERT INTO options_book_positions (occ, underlying, strategy, qty, avg_price, multiplier, opened_at) "
                        "VALUES (?,?,?,?,?,?,?)", (occ, parts.root, strategy, qty, price, multiplier, at))
                else:
                    n = pos.qty + qty
                    self.con.execute("UPDATE options_book_positions SET qty=?, avg_price=? WHERE occ=?",
                                     (n, (pos.avg_price * pos.qty + price * qty) / n, occ))
                owner = pos.strategy if pos else strategy
            else:  # Side.SELL (checked above)
                multiplier = pos.multiplier
                realized = (price - pos.avg_price) * qty * multiplier - fee
                left = pos.qty - qty
                if left <= QTY_EPS:
                    self.con.execute("DELETE FROM options_book_positions WHERE occ=?", (occ,))
                else:
                    self.con.execute("UPDATE options_book_positions SET qty=? WHERE occ=?", (left, occ))
                owner = pos.strategy
            self.con.execute(
                "INSERT INTO options_book_fills (client_order_id, occ, underlying, strategy, side, qty, price, multiplier, "
                "fee, filled_at, realized_pnl, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (client_order_id, occ, parts.root, owner, side.value, qty, price, multiplier, fee, at, realized,
                 _utc_iso(None)))
        return True

    def reconcile_flat(self, occ: str, reason: str, at: datetime | str | None = None, keep: float = 0.0,
                       write_off: bool = True) -> bool:
        """Bring a book position down to ``keep`` contracts (0: remove it)
        because the broker holds fewer (closed by hand, assigned, expired or
        exercised while nothing was watching). Writes an explicit
        ``side='reconcile'`` row so the change is auditable. The true P&L is
        unknown; ``write_off`` (default) books the removed contracts at price
        0, a total loss, so the daily stop and the drawdown latch see it
        (never a loss hidden at cost); otherwise at cost with realized 0.
        Never touches the broker. False when nothing changes."""
        pos = self.position(occ)
        if pos is None or keep >= pos.qty - QTY_EPS or keep < 0:
            return False
        at_s = _utc_iso(at)
        with self.con:
            if keep <= QTY_EPS:
                self.con.execute("DELETE FROM options_book_positions WHERE occ=?", (occ,))
            else:
                self.con.execute("UPDATE options_book_positions SET qty=? WHERE occ=?", (float(keep), occ))
            self.con.execute(
                "INSERT INTO options_book_fills (client_order_id, occ, underlying, strategy, side, qty, price, multiplier, "
                "fee, filled_at, realized_pnl, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"reconcile:{occ}:{at_s}:{reason}"[:200], occ, pos.underlying, pos.strategy, "reconcile",
                 pos.qty - keep, 0.0 if write_off else pos.avg_price, pos.multiplier, 0.0, at_s,
                 -(pos.qty - keep) * pos.avg_price * pos.multiplier if write_off else 0.0, _utc_iso(None)))
        return True

    def realized(self, start: str | None = None, end: str | None = None) -> float:
        """Realized P&L (fees included) of fills with start <= filled_at < end."""
        q, args = "SELECT COALESCE(SUM(realized_pnl),0) FROM options_book_fills WHERE 1=1", []
        if start is not None:
            q, args = q + " AND filled_at >= ?", args + [start]
        if end is not None:
            q, args = q + " AND filled_at < ?", args + [end]
        return float(self.con.execute(q, args).fetchone()[0])

    def get_state(self, key: str) -> str | None:
        r = self.con.execute("SELECT value FROM options_book_state WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def set_state(self, key: str, value: str) -> None:
        with self.con:
            self.con.execute("INSERT INTO options_book_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (key, value))


@dataclass
class OptionsLimits:
    capital_budget: float = 50_000      # the whole desk budget (same as stock Limits.capital_budget)
    sleeve_pct: float = 0.10            # owner-set: 10% now, at most 30% once the gates in section 3 pass
    max_trade_pct: float = 0.20         # premium of one entry, fraction of the sleeve budget
    max_positions: int = 3
    daily_loss_pct: float = 0.20        # realized today + unrealized, fraction of the sleeve budget
    dd_halt_pct: float = 0.35           # sleeve equity below its peak, fraction of the sleeve budget
    # Liquidity filters. Starting values only; options/config.py passes the tuned ones.
    max_spread_pct: float = 0.10        # (ask - bid) / mid
    min_mid: float = 0.20               # USD per share
    min_open_interest: int = 100
    min_volume: int = 50                # contracts traded today
    max_quote_age_s: float = 60.0
    max_dte_days: int = 7               # calendar days to expiry; no long-dated contracts in this sleeve
    entry_start: time = time(9, 30)     # ET
    entry_end: time = time(11, 30)      # ET, exclusive
    flat_by: time = time(15, 45)        # ET: everything is closed by then; never enter at/after it
    price_band: float = 0.10            # a buy's price may deviate this much from mid
    exit_band: float = 0.50             # a limit sell below mid * (1 - exit_band) is a fat finger
    halt_file: Path = Path("data/desk/HALT")  # shared kill switch with the stock book

    @property
    def sleeve_budget(self) -> float:
        return self.capital_budget * self.sleeve_pct

    @property
    def max_trade_premium(self) -> float:
        return self.sleeve_budget * self.max_trade_pct

    @property
    def daily_loss_stop(self) -> float:
        return self.sleeve_budget * self.daily_loss_pct

    @property
    def dd_halt(self) -> float:
        return self.sleeve_budget * self.dd_halt_pct

    def problems(self) -> list[str]:
        """Configuration errors; the engine refuses every entry while any exist."""
        out = []
        for name in ("capital_budget", "sleeve_pct", "max_trade_pct", "daily_loss_pct", "dd_halt_pct",
                     "max_spread_pct", "min_mid", "max_quote_age_s", "price_band", "exit_band"):
            v = getattr(self, name)
            if not (_num(v) and v > 0):
                out.append(f"{name} must be positive and finite, got {v!r}")
        if _num(self.sleeve_pct) and self.sleeve_pct > 0.30:
            out.append(f"sleeve_pct {self.sleeve_pct} exceeds the 30% ceiling in OPTIONS_PLAN.md section 3")
        for name in ("max_positions", "min_open_interest", "min_volume", "max_dte_days"):
            v = getattr(self, name)
            if not (isinstance(v, int) and not isinstance(v, bool) and v >= 0):
                out.append(f"{name} must be a non-negative int, got {v!r}")
        if not (self.entry_start < self.entry_end <= self.flat_by):
            out.append("need entry_start < entry_end <= flat_by")
        return out


@dataclass(frozen=True)
class SleeveState:
    budget: float
    realized_total: float
    realized_today: float
    unrealized: float
    open_premium: float     # cost basis of the open book positions
    daily_pnl: float        # realized today + unrealized
    equity: float           # budget + realized_total + unrealized
    n_open: int
    unmarked: tuple[str, ...]  # open positions without a usable mark (valued at cost)


def sleeve_state(con: sqlite3.Connection, limits: OptionsLimits, marks: Mapping[str, float],
                 now: datetime) -> SleeveState:
    """Sleeve P&L from the options tables. ``marks``: OCC -> per-share mark
    (mid). An open position without a finite mark >= 0 is valued at cost and
    listed in ``unmarked``. Read-only: does not touch the persisted peak."""
    book = OptionsBook(con)
    start, end = et_day_bounds(now.astimezone(ET).date())
    unreal, cost, unmarked = 0.0, 0.0, []
    positions = book.positions()
    for occ, p in positions.items():
        cost += p.cost
        m = marks.get(occ)
        if _num(m) and m >= 0:
            unreal += (float(m) - p.avg_price) * p.qty * p.multiplier
        else:
            unmarked.append(occ)
    total = book.realized()
    today = book.realized(start, end)
    return SleeveState(limits.sleeve_budget, total, today, unreal, cost, today + unreal,
                       limits.sleeve_budget + total + unreal, len(positions), tuple(unmarked))


def options_mismatches(book: OptionsBook | sqlite3.Connection, snapshot: AccountSnapshot) -> dict[str, str]:
    """{occ: problem} where the options book and the broker's option
    positions disagree; empty when they agree."""
    if not isinstance(book, OptionsBook):
        book = OptionsBook(book)
    mine = book.positions()
    theirs = {p.symbol: p for p in snapshot.positions if is_option(p) and abs(p.qty) > QTY_EPS}
    out: dict[str, str] = {}
    for occ in sorted(set(mine) | set(theirs)):
        m, t = mine.get(occ), theirs.get(occ)
        if m is not None and t is not None:
            if abs(m.qty - t.qty) > QTY_EPS:
                out[occ] = f"{occ}: quantity mismatch, book {m.qty:g} ({m.strategy}) vs broker {t.qty:g}"
        elif t is not None:
            out[occ] = f"{occ}: broker holds {t.qty:g} but the options book has no such position"
        else:
            out[occ] = f"{occ}: options book holds {m.qty:g} ({m.strategy}) but the broker has no such position"
    return out


def muse_blocked(con, now: datetime) -> dict[str, str]:
    """Underlyings Muse flags in force at ``now`` block option entries. Every
    ticker counts as a candidate for this sleeve (nothing is "held" here), so
    any ALERT or research risk flag on the underlying blocks: the 2026-10-21
    Muse downgrade covers stock entries only, so this sleeve keeps the
    pre-downgrade rule (``legacy_blocked``). Like the stock
    premarket, a failure to read the flags is logged and loses the veto; it
    does not stop trading."""
    try:
        from stockmoney.desk.bridge.policy import active_flags
        f = active_flags(con, now, ())
        return {**f.no_add, **f.legacy_blocked, **f.blocked}
    except Exception as e:
        log.warning("Muse flags unavailable for the options sleeve: %s", e)
        return {}


class OptionsRiskEngine:
    def __init__(self, limits: OptionsLimits, con: sqlite3.Connection):
        self.limits = limits
        self.con = con
        self.book = OptionsBook(con)
        # premium (USD) of buys sent during this run, by OCC (one engine = one session loop)
        self._reserved: dict[str, float] = {}

    def reserve(self, intent: OrderIntent, price: float) -> None:
        """Record a buy that was just submitted (``price`` per share) so later
        checks count it toward the budget and the position count until the
        book or the broker shows it."""
        if intent.side != Side.BUY or not (_num(price) and price > 0) or not _num(intent.qty):
            return
        self._reserved[intent.symbol] = self._reserved.get(intent.symbol, 0.0) + (
            float(intent.qty) * float(price) * OPTION_MULTIPLIER)

    def release(self, occ: str) -> None:
        """Forget a reservation once its order is booked or ended unfilled."""
        self._reserved.pop(occ, None)

    # ---------------------------------------------------------- switches
    def kill_switch(self) -> str | None:
        if Path(self.limits.halt_file).exists():
            return f"kill switch: {self.limits.halt_file} exists"
        if store.get_setting(self.con, "halt_new_entries", "0").strip() == "1":
            return "kill switch: setting halt_new_entries=1 (desk-wide pause)"
        if store.get_setting(self.con, OPTIONS_HALT_SETTING, "0").strip() == "1":
            return f"kill switch: setting {OPTIONS_HALT_SETTING}=1"
        return None

    def halted(self) -> str | None:
        """Reason of a latched drawdown halt, or None."""
        return self.book.get_state(HALT_KEY) or None

    def peak(self, equity: float | None = None) -> float:
        """Persisted sleeve equity peak (starts at the sleeve budget); raised
        and persisted when ``equity`` exceeds it. A peak measured against a
        different budget is discarded."""
        budget = repr(float(self.limits.sleeve_budget))
        raw = self.book.get_state(PEAK_KEY)
        if self.book.get_state(PEAK_BUDGET_KEY) != budget:
            raw = None
            self.book.set_state(PEAK_BUDGET_KEY, budget)
        pk = float(raw) if raw is not None and _num(raw) else self.limits.sleeve_budget
        if equity is not None and _num(equity) and equity > pk:
            pk = float(equity)
        if raw is None or float(raw) != pk:
            self.book.set_state(PEAK_KEY, repr(pk))
        return pk

    def rearm(self, state: SleeveState) -> None:
        """Human action: lift a latched drawdown halt and restart the peak
        from the current sleeve equity."""
        self.book.set_state(PEAK_BUDGET_KEY, repr(float(self.limits.sleeve_budget)))
        self.book.set_state(PEAK_KEY, repr(float(state.equity)))
        self.book.set_state(HALT_KEY, "")

    # -------------------------------------------------------------- check
    @staticmethod
    def _block(reason: str) -> RiskDecision:
        return RiskDecision(False, reason, None)

    def _marks(self, snapshot: AccountSnapshot | None, contract: OptionContract | None,
               marks: Mapping[str, float] | None) -> dict[str, float]:
        out: dict[str, float] = {}
        for p in (snapshot.positions if snapshot else []):
            if is_option(p) and _num(p.current_price):
                out[p.symbol] = float(p.current_price)
        if contract is not None and contract.mid is not None:
            out[contract.occ] = contract.mid
        out.update({k: v for k, v in (marks or {}).items()})
        return out

    def check(self, intent: OrderIntent, contract: OptionContract | None, snapshot: AccountSnapshot | None,
              now: datetime, *, open_orders: Sequence[BrokerOrder] | None = None,
              marks: Mapping[str, float] | None = None,
              blocked: Mapping[str, str] | None = None) -> RiskDecision:
        """Decide one option order.

        ``contract``: the latest quote of intent.symbol (required for a buy;
        a sell without one may still go out as a market order).
        ``snapshot``: a fresh broker account read (positions are the truth
        for what is held). ``open_orders``: the broker's working orders, so
        working option buys count toward the caps and working sells toward
        the held qty. ``marks``: OCC -> per-share mark overriding the broker's
        current_price. ``blocked``: Muse-blocked underlyings; None reads them
        from the bridge tables (muse_blocked)."""
        if (why := self._invalid(intent, now)):
            return self._block(f"invalid option intent: {why}")
        if contract is not None and contract.occ != intent.symbol:
            return self._block(f"quote is for {contract.occ}, intent is for {intent.symbol}")
        if intent.side == Side.SELL:
            return self._check_exit(intent, contract, snapshot, open_orders or [])
        return self._check_entry(intent, contract, snapshot, now, list(open_orders or []), marks, blocked)

    @staticmethod
    def _invalid(intent: OrderIntent, now: datetime) -> str | None:
        if intent.asset_class != AssetClass.OPTION:
            return f"asset class {intent.asset_class.value}"
        try:
            parse_occ(intent.symbol)
        except ValueError as e:
            return str(e)
        if " " in intent.symbol:
            return "symbol must use the unpadded broker spelling"
        if intent.notional is not None:
            return "notional is not allowed"
        q = intent.qty
        if not (_num(q) and q >= 1 and float(q) == int(q)):
            return f"qty must be whole contracts >= 1, got {q!r}"
        if intent.order_type not in (OrderType.MARKET, OrderType.LIMIT):
            return f"order type {intent.order_type.value} (market or limit only)"
        if intent.tif != TIF.DAY:
            return f"TIF {intent.tif.value} (day only)"
        if intent.stop_price is not None:
            return "stop_price is not allowed"
        if intent.order_type == OrderType.LIMIT and not (_num(intent.limit_price) and intent.limit_price > 0):
            return f"limit_price must be positive and finite, got {intent.limit_price!r}"
        if intent.order_type == OrderType.MARKET and intent.limit_price is not None:
            return "limit_price on a market order"
        if not isinstance(now, datetime) or now.tzinfo is None:
            return "now must be a timezone-aware datetime"
        return None

    # ---- exits: always allowed up to what is held
    def _check_exit(self, intent: OrderIntent, contract: OptionContract | None,
                    snapshot: AccountSnapshot | None, open_orders: Sequence[BrokerOrder]) -> RiskDecision:
        if snapshot is not None:
            held = sum(p.qty for p in snapshot.positions if p.symbol == intent.symbol and is_option(p))
            source = "broker"
        else:
            pos = self.book.position(intent.symbol)
            held, source = (pos.qty if pos else 0.0), "options book (no broker snapshot)"
        working = sum((o.qty or 0) - (o.filled_qty or 0) for o in open_orders
                      if o.symbol == intent.symbol and o.side == Side.SELL and o.status in _LIVE
                      and o.client_order_id != intent.client_order_id)
        qty = float(intent.qty)
        if not _num(held) or held <= QTY_EPS:
            return self._block(f"sell blocked: no {intent.symbol} position at the {source} (no sell to open)")
        if qty + working > held + QTY_EPS:
            return self._block(f"sell blocked: qty {qty:g} plus working sells {working:g} exceeds held {held:g} "
                               f"of {intent.symbol} (no sell to open)")
        if intent.order_type == OrderType.LIMIT:
            mid = contract.mid if contract is not None else None
            if mid is None:
                return self._block("sell blocked: limit exit needs a sane quote to check its price; "
                                   "send a market sell instead")
            floor_px = mid * (1 - self.limits.exit_band)
            if intent.limit_price < floor_px:
                return self._block(f"sell blocked: limit {intent.limit_price:g} is more than "
                                   f"{self.limits.exit_band:.0%} below mid {mid:g} (fat finger?)")
        return RiskDecision(True, "exit allowed: reduces exposure", intent)

    # ---- entries: every rule
    def _check_entry(self, intent: OrderIntent, k: OptionContract | None, snapshot: AccountSnapshot | None,
                     now: datetime, open_orders: list[BrokerOrder], marks: Mapping[str, float] | None,
                     blocked: Mapping[str, str] | None) -> RiskDecision:
        l = self.limits
        if (probs := l.problems()):
            return self._block("buy blocked: invalid limits: " + "; ".join(probs))
        if (ks := self.kill_switch()):
            return self._block(f"buy blocked: {ks}")
        if (h := self.halted()):
            return self._block(f"buy blocked: sleeve halted until re-armed by the owner ({h})")
        if snapshot is None:
            return self._block("buy blocked: no broker snapshot")
        if snapshot.trading_blocked:
            return self._block("buy blocked: broker reports trading_blocked")
        et = now.astimezone(ET)
        if et.weekday() >= 5:
            return self._block(f"buy blocked: {et:%A} is not a trading day")
        t = et.time()
        if t >= l.flat_by:
            return self._block(f"buy blocked: {t:%H:%M} ET is at/after the forced-flat time {l.flat_by:%H:%M}")
        if not (l.entry_start <= t < l.entry_end):
            return self._block(f"buy blocked: {t:%H:%M} ET is outside the entry window "
                               f"{l.entry_start:%H:%M}-{l.entry_end:%H:%M}")
        if k is None:
            return self._block("buy blocked: no quote for the contract")
        parts = parse_occ(intent.symbol)
        if k.multiplier != OPTION_MULTIPLIER or parts.root != k.underlying.upper():
            return self._block(f"buy blocked: non-standard contract (root {parts.root}, underlying {k.underlying}, "
                               f"multiplier {k.multiplier})")
        if k.expiry <= et.date():  # 0DTE too: an ITM option held into the close is exercised into stock
            return self._block(f"buy blocked: contract expires {k.expiry} (today or earlier; no 0DTE live buy)")
        if (k.expiry - et.date()).days > l.max_dte_days:
            return self._block(f"buy blocked: expiry {k.expiry} is more than {l.max_dte_days} days out")
        muse = blocked if blocked is not None else muse_blocked(self.con, now)
        if (why := {str(u).upper(): r for u, r in muse.items()}.get(k.underlying.upper())):
            return self._block(f"buy blocked: Muse flag on {k.underlying}: {why}")
        if (bad := options_mismatches(self.book, snapshot)):
            return self._block("buy blocked: options book does not match the broker: " + "; ".join(bad.values())[:400])

        # liquidity and quote quality
        mid, spread = k.mid, k.spread_pct
        if mid is None or spread is None or not (_num(k.bid) and _num(k.ask)) or k.bid <= 0:
            return self._block(f"buy blocked: no sane two-sided quote (bid {k.bid}, ask {k.ask})")
        if k.as_of is None or k.as_of.tzinfo is None:
            return self._block("buy blocked: quote has no timestamp")
        age = (now - k.as_of).total_seconds()
        if not (-5.0 <= age <= l.max_quote_age_s):
            return self._block(f"buy blocked: quote is {age:.0f}s old (limit {l.max_quote_age_s:.0f}s)")
        if spread > l.max_spread_pct:
            return self._block(f"buy blocked: spread {spread:.1%} of mid exceeds {l.max_spread_pct:.1%}")
        if mid < l.min_mid:
            return self._block(f"buy blocked: mid {mid:g} below minimum {l.min_mid:g}")
        if not (_num(k.open_interest) and k.open_interest >= l.min_open_interest):
            return self._block(f"buy blocked: open interest {k.open_interest} below {l.min_open_interest}")
        if not (_num(k.volume) and k.volume >= l.min_volume):
            return self._block(f"buy blocked: volume today {k.volume} below {l.min_volume}")
        px = float(intent.limit_price) if intent.order_type == OrderType.LIMIT else float(k.ask)
        if abs(px / mid - 1.0) > l.price_band:
            return self._block(f"buy blocked: price {px:g} deviates {abs(px / mid - 1):.1%} from mid {mid:g} "
                               f"(limit {l.price_band:.0%})")

        # sleeve P&L, daily stop, drawdown latch
        st = sleeve_state(self.con, l, self._marks(snapshot, k, marks), now)
        if st.unmarked:
            return self._block("buy blocked: open positions without a usable mark: " + ", ".join(st.unmarked))
        if not all(_num(x) for x in (st.daily_pnl, st.equity)):
            return self._block("buy blocked: sleeve P&L is not finite")
        if st.daily_pnl <= -l.daily_loss_stop:
            return self._block(f"buy blocked: daily loss {st.daily_pnl:,.0f} reached the stop "
                               f"-{l.daily_loss_stop:,.0f}")
        pk = self.peak(st.equity)
        if pk - st.equity >= l.dd_halt:
            why = (f"sleeve equity {st.equity:,.0f} is {pk - st.equity:,.0f} below its peak {pk:,.0f} "
                   f"(limit {l.dd_halt:,.0f}) at {now.isoformat(timespec='seconds')}")
            self.book.set_state(HALT_KEY, why)
            return self._block(f"buy blocked: drawdown halt latched: {why}")

        # positions and premium at risk
        book = self.book.positions()
        held = set(book) | {p.symbol for p in snapshot.positions if is_option(p) and abs(p.qty) > QTY_EPS}
        working = {}
        for o in open_orders:
            if is_option(o) and o.side == Side.BUY and o.status in _LIVE and o.client_order_id != intent.client_order_id:
                rest = max((o.qty or 0) - (o.filled_qty or 0), 0.0)
                price = o.limit_price if _num(o.limit_price) else (k.ask if o.symbol == k.occ else 0.0)
                working[o.symbol] = working.get(o.symbol, 0.0) + rest * float(price) * OPTION_MULTIPLIER
        if intent.symbol in held or intent.symbol in working or intent.symbol in self._reserved:
            return self._block(f"buy blocked: {intent.symbol} is already held or has a working buy "
                               f"(one entry per contract)")
        n_pos = len(held | set(working) | set(self._reserved))
        if n_pos >= l.max_positions:
            return self._block(f"buy blocked: {n_pos} positions/working entries already (max {l.max_positions})")
        unknown = sum(abs(p.market_value) for p in snapshot.positions
                      if is_option(p) and p.symbol not in book and _num(p.market_value))
        at_risk = st.open_premium + unknown + sum(working.values()) + sum(self._reserved.values())
        per_contract = px * OPTION_MULTIPLIER
        caps = [("per-trade premium", l.max_trade_premium, f"{l.max_trade_pct:.0%} of sleeve budget"),
                ("sleeve budget", l.sleeve_budget - at_risk,
                 f"budget {l.sleeve_budget:,.0f} less premium at risk {at_risk:,.0f}")]
        qty = float(intent.qty)
        cuts = []
        for label, room, desc in caps:
            if not _num(room) or room < per_contract:
                return self._block(f"buy blocked: {label} cap reached ({desc}; room {max(room, 0):,.0f} < "
                                   f"one contract {per_contract:,.0f})")
            fit = float(floor(room / per_contract + 1e-9))
            if qty > fit:
                cuts.append(f"cut to {fit:g} contracts by {label} cap ({desc})")
                qty = fit
        if not cuts:
            return RiskDecision(True, "ok", intent)
        note = f"[options risk resized qty {intent.qty:g}->{qty:g}: {'; '.join(cuts)}]"
        new = replace(intent, qty=qty, reason=f"{intent.reason} {note}".strip())
        return RiskDecision(True, f"resized: {'; '.join(cuts)}", new)
