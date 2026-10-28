"""Pre-trade risk engine. Safety-critical: a bug here is worse than no feature.

Design rules:
- Anything that REDUCES exposure (a sell of an existing long, up to the held
  quantity) is allowed regardless of kill switch, drawdown mode, sleeve breaker
  or the per-run order cap. The system must always be able to exit. The only
  check applied to sells is a price sanity check on the order's own limit price,
  plus the no-shorting check (a sell larger than the held quantity is blocked).
- Anything that INCREASES exposure (a buy) must pass every rule, and is resized
  (never silently) when a cap binds. A resized order carries a reason saying
  what was cut and why.
- Option intents and option positions are out of scope: an option intent is
  blocked here (the options sleeve has its own OptionsRiskEngine) and option
  positions in the snapshot are ignored (not exposure, not a held stock).
- System equity is capital_budget + realized + unrealized P&L of the system's
  own ledger. The broker's total equity is deliberately ignored: a paper
  account may hold $100k but the system behaves as if it only has
  ``capital_budget``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from pathlib import Path

from stockmoney.desk import store, veto
from stockmoney.desk.execution.isolation import without_options
from stockmoney.desk.execution.ledger import (
    Ledger, is_crypto_symbol, norm,
)
from stockmoney.desk.execution.types import (
    AccountSnapshot, AssetClass, OrderIntent, RiskDecision, Side,
)
from stockmoney.desk.universe import STOCKS

# Leverage multiple per ticker, used only by the beta-adjusted caps below.
# Only leveraged ETFs that already appear in this repo (data/watchlist.py,
# desk/muse_ops.py, docs/desk/PHASE1_RESEARCH.md) are listed. Inverse funds
# are counted by absolute leverage (gross). Unknown tickers count as 1.0.
DEFAULT_LEVERAGE: dict[str, float] = {"TQQQ": 3.0, "SOXL": 3.0, "SOXS": 3.0}

# "US large-cap growth" group for max_growth_group_exposure: the universe's
# tech / semiconductor / communication names (XLK, SMH, XLC), AMZN and TSLA
# (large-cap growth filed under XLY), and the broad growth ETFs. PHASE1_RESEARCH
# 6.3: the desk and TQQQ are the same risk factor at two leverages (monthly
# correlation 0.71), so the group is measured beta-adjusted. Inverse funds
# (SOXS) are not members: they hedge this factor, they do not add to it.
GROWTH_GROUP: frozenset[str] = frozenset(
    {sym for sym, sec in STOCKS.items() if sec in ("XLK", "SMH", "XLC")}
    | {"AMZN", "TSLA", "QQQ", "TQQQ", "SOXL", "XLK", "SMH", "XLC"}
)

MIN_ORDER_NOTIONAL = 1.0  # a buy resized below this is blocked instead
CRYPTO_SELL_TOL = 1e-6
EQUITY_SELL_TOL = 1e-9
HWM_KEY = "equity_hwm"
HWM_BUDGET_KEY = "equity_hwm_budget"  # the capital_budget the stored peak was measured against
SLEEVE_PEAK_KEY = "sleeve_peak_pnl:"


@dataclass
class Limits:
    capital_budget: float = 50_000      # the system treats only this much as its own money
    max_order_notional_pct: float = 0.25
    max_position_pct: float = 0.20      # OPERATING_PLAN.md: at most 20% of equity in one name
    max_gross_exposure_pct: float = 1.0
    max_crypto_exposure_pct: float = 0.30
    max_orders_per_run: int = 20
    price_band_equity: float = 0.10     # order price deviating from ref price by more than this is rejected
    price_band_crypto: float = 0.20
    dd_reduce: float = 0.20             # system equity drawdown -> new orders are halved
    dd_halt: float = 0.35               # -> no new entries (exits and stops continue)
    # Additions beyond the spec (defaults keep the spec'd constructor valid):
    sleeve_dd_halt: float = 0.08        # a strategy whose P&L falls this fraction of capital_budget below its peak is disabled for new entries
    halt_file: Path = Path("data/desk/HALT")  # existence = kill switch
    # Beta-adjusted caps (fractions of system equity; each position counts at
    # value * leverage). None = disabled, behavior identical to before.
    # OWNER DECISION REQUIRED: the numeric values are deliberately left None.
    # Set them (from the PHASE1_RESEARCH bootstrap results) BEFORE enabling a
    # TQQQ-style strategy live. See docs/desk/OPERATING_PLAN.md.
    max_beta_adj_exposure: float | None = None
    max_growth_group_exposure: float | None = None
    leverage: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_LEVERAGE))
    # Research veto (desk/veto.py): a NEW BUY is blocked when this many independent
    # research sources flagged the symbol elevated/critical within veto_days
    # trading days. Buys only; sells and stops never consult it.
    veto_days: int = veto.DAYS
    veto_min_sources: int = veto.MIN_SOURCES


def _is_num(x: float | None) -> bool:
    return x is not None and math.isfinite(x)


def leverage_of(symbol: str, table: dict[str, float] | None = None) -> float:
    """Leverage multiple of a ticker (absolute value); unknown tickers 1.0.
    ``table`` keys must already be normalized with ``norm``."""
    t = {norm(k): v for k, v in DEFAULT_LEVERAGE.items()} if table is None else table
    v = t.get(norm(symbol))
    return abs(v) if _is_num(v) and v != 0 else 1.0


class RiskEngine:
    def __init__(self, limits: Limits, con):
        self.limits = limits
        self.con = con
        self.ledger = Ledger(con)  # also creates the ledger/risk tables
        # Notional of buys already sent during this run (one engine = one run).
        # Added to existing exposure so caps hold across several orders that
        # are all checked against the same start-of-run snapshot.
        self._pending: dict[str, float] = {}
        self._pending_crypto: dict[str, bool] = {}

    def reserve(self, intent: OrderIntent, price: float) -> None:
        """Record a buy that was just submitted so later checks in the same run
        count it toward the per-position, gross and crypto caps."""
        if intent.side != Side.BUY:
            return
        n = intent.qty * price if intent.qty is not None else intent.notional
        if not (_is_num(n) and n > 0):
            return
        key = norm(intent.symbol)
        self._pending[key] = self._pending.get(key, 0.0) + n
        self._pending_crypto[key] = intent.asset_class == AssetClass.CRYPTO

    # ------------------------------------------------------------ equity
    @staticmethod
    def _prices(snapshot: AccountSnapshot) -> dict[str, float]:
        return {p.symbol: p.current_price for p in without_options(snapshot).positions}

    def system_equity(self, snapshot: AccountSnapshot) -> float:
        """capital_budget + ledger realized + ledger unrealized. NOT broker equity."""
        return self.ledger.system_equity(self.limits.capital_budget, self._prices(snapshot))

    def high_water_mark(self, equity: float | None = None) -> float:
        """Persisted equity peak (starts at capital_budget). Passing the current
        equity raises and persists the peak when it is exceeded. A peak that
        was measured against a different capital_budget is discarded and the
        peak restarts at the current budget."""
        budget = repr(float(self.limits.capital_budget))
        raw = self.ledger.get_state(HWM_KEY)
        stored_budget = self.ledger.get_state(HWM_BUDGET_KEY)
        if stored_budget is None:
            self.ledger.set_state(HWM_BUDGET_KEY, budget)  # legacy peak: adopt it for the current budget
        elif stored_budget != budget:
            raw = None
            self.ledger.set_state(HWM_BUDGET_KEY, budget)
        hwm = float(raw) if raw is not None else self.limits.capital_budget
        if equity is not None and equity > hwm:
            hwm = equity
        if raw is None or float(raw) != hwm:
            self.ledger.set_state(HWM_KEY, repr(hwm))
        return hwm

    # --------------------------------------------------------- kill switch
    def kill_switch(self) -> str | None:
        """Reason string if new entries are switched off, else None."""
        if Path(self.limits.halt_file).exists():
            return f"kill switch: {self.limits.halt_file} exists"
        if store.get_setting(self.con, "halt_new_entries", "0").strip() == "1":
            return "kill switch: setting halt_new_entries=1"
        return None

    # ---------------------------------------------------------------- mode
    def mode(self, snapshot: AccountSnapshot) -> tuple[str, str]:
        """("normal"|"reduced"|"halted", reason). Updates the persisted high-water mark."""
        equity = self.system_equity(snapshot)
        hwm = self.high_water_mark(equity)
        dd = 1.0 - equity / hwm if hwm > 0 else 1.0
        info = f"system equity {equity:,.0f} vs peak {hwm:,.0f} (drawdown {dd:.1%})"
        if (k := self.kill_switch()):
            return "halted", k
        if equity <= 0 or dd >= self.limits.dd_halt:
            return "halted", f"{info} >= halt threshold {self.limits.dd_halt:.0%}"
        if dd >= self.limits.dd_reduce:
            return "reduced", f"{info} >= reduce threshold {self.limits.dd_reduce:.0%}"
        return "normal", info

    # -------------------------------------------------------- sleeve breaker
    def sleeve_breaker(self, strategy: str, snapshot: AccountSnapshot) -> tuple[bool, str]:
        """(tripped, reason). Sleeve P&L = ledger realized + unrealized for that
        strategy; tripped when it is more than sleeve_dd_halt * capital_budget
        below the sleeve's persisted peak P&L (peak starts at 0)."""
        pnl = self.ledger.realized_pnl(strategy) + self.ledger.unrealized_pnl(self._prices(snapshot), strategy)
        key = SLEEVE_PEAK_KEY + strategy
        raw = self.ledger.get_state(key)
        peak = max(float(raw) if raw is not None else 0.0, pnl)
        if raw is None or float(raw) != peak:
            self.ledger.set_state(key, repr(peak))
        dd = (peak - pnl) / self.limits.capital_budget
        if dd > self.limits.sleeve_dd_halt:
            return True, (f"sleeve '{strategy}' circuit breaker: P&L {pnl:,.0f} is {dd:.1%} of budget below "
                          f"its peak {peak:,.0f} (limit {self.limits.sleeve_dd_halt:.0%})")
        return False, ""

    def reset_sleeve_peak(self, strategy: str, snapshot: AccountSnapshot) -> None:
        """Human action: re-arm a tripped sleeve from its current P&L."""
        pnl = self.ledger.realized_pnl(strategy) + self.ledger.unrealized_pnl(self._prices(snapshot), strategy)
        self.ledger.set_state(SLEEVE_PEAK_KEY + strategy, repr(pnl))

    # ------------------------------------------------------------ exposure
    def _exposure(self, snapshot: AccountSnapshot) -> tuple[dict[str, float], float, float]:
        """(value by normalized symbol, gross, crypto). Broker values win over
        ledger cost so unknown broker positions are counted conservatively."""
        values: dict[str, float] = {}
        crypto: dict[str, bool] = {}
        for sym, pos in self.ledger.positions().items():
            values[norm(sym)] = pos.qty * pos.avg_price
            crypto[norm(sym)] = is_crypto_symbol(sym)
        for p in without_options(snapshot).positions:
            values[norm(p.symbol)] = abs(p.market_value) if p.market_value else abs(p.qty * p.current_price)
            crypto[norm(p.symbol)] = p.asset_class == AssetClass.CRYPTO
        for key, n in self._pending.items():
            values[key] = values.get(key, 0.0) + n
            crypto[key] = crypto.get(key, False) or self._pending_crypto.get(key, False)
        gross = sum(values.values())
        return values, gross, sum(v for k, v in values.items() if crypto[k])

    # --------------------------------------------------------------- check
    def check(self, intent: OrderIntent, snapshot: AccountSnapshot, ref_price: float,
              orders_this_run: int, market_price: float | None = None) -> RiskDecision:
        """``ref_price``: the reference price for the price band (previous
        close / signal price) and for sizing. ``market_price`` (optional): a
        live quote; for buys it is held against the band and, when valid, it
        replaces ref_price for sizing, so caps are measured at the price the
        order will actually pay."""
        if intent.asset_class == AssetClass.OPTION:
            return self._block("option intents go through the options sleeve risk engine, not the stock book")
        if (intent.qty is None) == (intent.notional is None):
            return self._block("invalid intent: exactly one of qty/notional must be set")
        if (intent.qty if intent.qty is not None else intent.notional) <= 0:
            return self._block("invalid intent: qty/notional must be positive")
        if intent.side == Side.SELL:
            return self._check_sell(intent, snapshot, ref_price)
        return self._check_buy(intent, snapshot, ref_price, orders_this_run, market_price)

    @staticmethod
    def _block(reason: str) -> RiskDecision:
        return RiskDecision(False, reason, None)

    def _band(self, intent: OrderIntent) -> float:
        l = self.limits
        return l.price_band_crypto if intent.asset_class == AssetClass.CRYPTO else l.price_band_equity

    def _band_violation(self, label: str, price: float | None, ref_price: float, band: float) -> str | None:
        if price is None:
            return None
        if not _is_num(price) or price <= 0:
            return f"price band: invalid {label} {price}"
        dev = abs(price / ref_price - 1.0)
        if dev > band:
            return f"price band: {label} {price:g} deviates {dev:.1%} from ref {ref_price:g} (limit {band:.0%})"
        return None

    # ---- reduce-only orders
    def _check_sell(self, intent: OrderIntent, snapshot: AccountSnapshot, ref_price: float) -> RiskDecision:
        # Band applies to the order's own limit price only. Protective stop
        # prices and market sells are never band-checked: a gap-down exit must
        # not be blocked by a price check.
        if intent.limit_price is not None:
            if not (_is_num(ref_price) and ref_price > 0):
                return self._block(f"sell blocked: invalid ref_price {ref_price} for limit-price check")
            if (v := self._band_violation("limit price", intent.limit_price, ref_price, self._band(intent))):
                return self._block(v)
        held = next((p.qty for p in without_options(snapshot).positions
                     if norm(p.symbol) == norm(intent.symbol)), 0.0)
        if intent.qty is not None:
            qty = intent.qty
        else:
            if not (_is_num(ref_price) and ref_price > 0):
                return self._block(f"sell blocked: invalid ref_price {ref_price} to convert notional to qty")
            qty = intent.notional / ref_price
        tol = CRYPTO_SELL_TOL if intent.asset_class == AssetClass.CRYPTO else EQUITY_SELL_TOL
        if held <= tol:
            return self._block(f"sell blocked: no {intent.symbol} position at the broker (no shorting)")
        if qty > held + tol:
            return self._block(f"sell blocked: qty {qty:g} exceeds held {held:g} of {intent.symbol} (no shorting)")
        return RiskDecision(True, "exit allowed: reduces exposure", intent)

    # ---- exposure-increasing orders
    def _check_buy(self, intent: OrderIntent, snapshot: AccountSnapshot, ref_price: float,
                   orders_this_run: int, market_price: float | None) -> RiskDecision:
        l = self.limits
        if (k := self.kill_switch()):
            return self._block(f"buy blocked: {k}")
        if snapshot.trading_blocked:
            return self._block("buy blocked: broker reports trading_blocked")
        md, md_reason = self.mode(snapshot)
        if md == "halted":
            return self._block(f"buy blocked: halted ({md_reason})")
        if intent.strategy:
            tripped, why = self.sleeve_breaker(intent.strategy, snapshot)
            if tripped:
                return self._block(f"buy blocked: {why}")
        vt = veto.check(self.con, intent.symbol, days=l.veto_days, min_sources=l.veto_min_sources,
                        client_order_id=intent.client_order_id)
        if vt.blocked:
            veto.record(self.con, intent.symbol, vt)
            return self._block(f"buy blocked: {vt.reason}")
        d = self._size_buy(intent, snapshot, ref_price, orders_this_run, market_price, md, md_reason)
        if d.allow and vt.unconfirmed:  # only buys that actually pass; a later block is not "let through"
            veto.record_unconfirmed(self.con, intent.symbol, vt, intent.client_order_id)
        return d

    def _size_buy(self, intent: OrderIntent, snapshot: AccountSnapshot, ref_price: float,
                  orders_this_run: int, market_price: float | None, md: str, md_reason: str) -> RiskDecision:
        l = self.limits
        owner = self.ledger.owner(intent.symbol)
        if owner is not None and intent.strategy and owner != intent.strategy:
            return self._block(f"buy blocked: {intent.symbol} is held by strategy '{owner}', "
                               f"one symbol belongs to one strategy")
        if orders_this_run >= l.max_orders_per_run:
            return self._block(f"buy blocked: {orders_this_run} orders already this run (max {l.max_orders_per_run})")
        if not (_is_num(ref_price) and ref_price > 0):
            return self._block(f"buy blocked: invalid ref_price {ref_price}")
        band = self._band(intent)
        for label, px in (("limit price", intent.limit_price), ("stop price", intent.stop_price),
                          ("market price", market_price)):
            if (v := self._band_violation(label, px, ref_price, band)):
                return self._block(f"buy blocked: {v}")

        equity = self.system_equity(snapshot)
        if not _is_num(equity) or equity <= 0:
            return self._block(f"buy blocked: system equity {equity:,.0f} <= 0")
        price = market_price if (_is_num(market_price) and market_price > 0) else ref_price
        values, gross, crypto_exp = self._exposure(snapshot)
        n0 = intent.qty * price if intent.qty is not None else intent.notional
        n = n0
        cuts: list[str] = []
        if md == "reduced":
            n *= 0.5
            cuts.append(f"halved by drawdown mode ({md_reason})")
        caps = [
            ("per-order", l.max_order_notional_pct * equity,
             f"{l.max_order_notional_pct:.0%} of equity"),
            ("per-position", l.max_position_pct * equity - values.get(norm(intent.symbol), 0.0),
             f"{l.max_position_pct:.0%} of equity less existing {values.get(norm(intent.symbol), 0.0):,.0f}"),
            ("gross exposure", l.max_gross_exposure_pct * equity - gross,
             f"{l.max_gross_exposure_pct:.0%} of equity less existing gross {gross:,.0f}"),
        ]
        if intent.asset_class == AssetClass.CRYPTO:
            caps.append(("crypto exposure", l.max_crypto_exposure_pct * equity - crypto_exp,
                         f"{l.max_crypto_exposure_pct:.0%} of equity less existing crypto {crypto_exp:,.0f}"))
        if l.max_beta_adj_exposure is not None or l.max_growth_group_exposure is not None:
            # Beta-adjusted caps. The cap is in leveraged units, so room in
            # order notional is (cap - existing) / leverage. These only add
            # entries to the cap list: a buy can shrink or block, never grow.
            lev = {norm(k): v for k, v in l.leverage.items()}
            lv = leverage_of(intent.symbol, lev)
            if l.max_beta_adj_exposure is not None:
                adj = sum(v * leverage_of(k, lev) for k, v in values.items())
                caps.append(("beta-adjusted exposure", (l.max_beta_adj_exposure * equity - adj) / lv,
                             f"{l.max_beta_adj_exposure:.0%} of equity less existing beta-adjusted {adj:,.0f}, "
                             f"{intent.symbol} counts x{lv:g}"))
            members = {norm(g) for g in GROWTH_GROUP}
            if l.max_growth_group_exposure is not None and norm(intent.symbol) in members:
                gadj = sum(v * leverage_of(k, lev) for k, v in values.items() if k in members)
                caps.append(("growth-group exposure", (l.max_growth_group_exposure * equity - gadj) / lv,
                             f"{l.max_growth_group_exposure:.0%} of equity less existing growth-group "
                             f"beta-adjusted {gadj:,.0f}, {intent.symbol} counts x{lv:g}"))
        for label, room, desc in caps:
            if room < MIN_ORDER_NOTIONAL:
                return self._block(f"buy blocked: {label} cap reached ({desc}; room {max(room, 0):,.0f})")
            if n > room * (1 + 1e-9):
                n = room
                cuts.append(f"cut to {room:,.0f} by {label} cap ({desc})")
        return self._finish_buy(intent, price, n0, n, cuts)

    def _finish_buy(self, intent: OrderIntent, ref_price: float, n0: float, n: float,
                    cuts: list[str]) -> RiskDecision:
        if n >= n0 * (1 - 1e-9):
            return RiskDecision(True, "ok", intent)
        if intent.qty is not None:
            q = n / ref_price
            if intent.asset_class == AssetClass.CRYPTO:
                q = math.floor(q * 1e8 + 1e-6) / 1e8
            elif float(intent.qty).is_integer():
                q = float(math.floor(q + 1e-9))  # whole shares stay whole
            if q <= 0 or q * ref_price < MIN_ORDER_NOTIONAL:
                return self._block(f"buy blocked: resized to nothing ({'; '.join(cuts)})")
            new = replace(intent, qty=q, reason=self._reason(intent, f"qty {intent.qty:g}->{q:g}", cuts))
        else:
            c = math.floor(n * 100 + 1e-6) / 100
            if c < MIN_ORDER_NOTIONAL:
                return self._block(f"buy blocked: resized to nothing ({'; '.join(cuts)})")
            new = replace(intent, notional=c, reason=self._reason(intent, f"notional {intent.notional:,.2f}->{c:,.2f}", cuts))
        return RiskDecision(True, f"resized: {'; '.join(cuts)}", new)

    @staticmethod
    def _reason(intent: OrderIntent, change: str, cuts: list[str]) -> str:
        note = f"[risk resized {change}: {'; '.join(cuts)}]"
        return f"{intent.reason} {note}".strip()
