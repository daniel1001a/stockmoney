"""Options risk-control track (CLAUDE.md section 10.1): stop/take-profit
trigger evaluation for a single held position. Pure functions over plain
dataclasses, no DB access, so every rule is independently unit-testable --
same pattern as `ev_gate.py` and `options_math.py`.

v1 uses the fixed starting parameters CLAUDE.md section 16 lists as
"not final, pending calibration" (walk-forward grid search over these comes
later, once enough options-chain history has accumulated -- 110 rows in
`options_derived_daily` as of 2026-07-10 is nowhere near enough).

Two triggers -- regime invalidation and the dynamic-EV take-profit -- need a
*production* (live, not backtest-only) regime label and EV estimate that
don't exist yet (module A/B only run inside the walk-forward backtest today).
Rather than bolt on an under-reviewed production-inference path here, this
module accepts those as optional pre-computed inputs on `MarketSnapshot`: the
trigger simply doesn't fire (and is called out in `RiskAssessment.notes`)
until that wiring exists. CLAUDE.md itself flags regime detection as the
highest-risk module for look-ahead bias, so it deserves its own reviewed
change, not one folded into a risk-dashboard feature.

Applicability by side:
- Buyer risk controls (price stop, premium stop, fixed take-profit, dynamic
  EV take-profit) only apply to `side == "long"`.
- The seller early-close rule only applies to `side == "short"`.
- Regime invalidation applies to both.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

# v1 starting values, CLAUDE.md section 10.1 / section 16.
PRICE_STOP_Z = -2.0
PREMIUM_STOP_PCT = -0.50
TAKE_PROFIT_PCT = 1.00
SELLER_MIN_CAPTURE = 0.50
SELLER_MAX_CAPTURE = 0.80
YELLOW_PROXIMITY = 0.8  # "approaching" = 80% of the way to a red threshold

GREEN, YELLOW, RED = "green", "yellow", "red"
_LIGHT_RANK = {GREEN: 0, YELLOW: 1, RED: 2}


@dataclass
class OptionPosition:
    position_id: str
    symbol: str
    option_right: str    # 'call' | 'put'
    side: str             # 'long' | 'short'
    entry_date: date
    entry_underlying_price: float
    entry_premium: float
    entry_iv: float | None = None
    regime_at_entry: int | None = None

    def __post_init__(self):
        if self.option_right not in ("call", "put"):
            raise ValueError(f"option_right must be 'call' or 'put', got {self.option_right!r}")
        if self.side not in ("long", "short"):
            raise ValueError(f"side must be 'long' or 'short', got {self.side!r}")


@dataclass
class MarketSnapshot:
    as_of_date: date
    underlying_price: float
    current_premium: float | None = None
    current_regime: int | None = None
    ev_of_continuing: float | None = None  # module B's compute_ev(), reverse-used per section 10.1


@dataclass
class RiskTrigger:
    kind: str
    light: str  # 'yellow' | 'red'
    detail: str


@dataclass
class RiskAssessment:
    position_id: str
    as_of_date: date
    light: str
    triggers: list[RiskTrigger] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _price_stop_trigger(position: OptionPosition, snapshot: MarketSnapshot) -> RiskTrigger | None:
    if position.entry_iv is None or position.entry_iv <= 0:
        return None
    days_held = max((snapshot.as_of_date - position.entry_date).days, 1)
    sigma_price = position.entry_underlying_price * position.entry_iv * math.sqrt(days_held / 365)
    if sigma_price <= 0:
        return None

    if position.option_right == "call":
        adverse_move = snapshot.underlying_price - position.entry_underlying_price
    else:
        adverse_move = position.entry_underlying_price - snapshot.underlying_price
    z = adverse_move / sigma_price

    if z <= PRICE_STOP_Z:
        return RiskTrigger("price_stop", RED, f"underlying moved {z:.2f} std devs against entry (threshold {PRICE_STOP_Z})")
    if z <= PRICE_STOP_Z * YELLOW_PROXIMITY:
        return RiskTrigger("price_stop", YELLOW, f"underlying at {z:.2f} std devs against entry, approaching {PRICE_STOP_Z}")
    return None


def _premium_stop_trigger(position: OptionPosition, snapshot: MarketSnapshot) -> RiskTrigger | None:
    if not snapshot.current_premium or position.entry_premium <= 0:
        return None
    pct = (snapshot.current_premium - position.entry_premium) / position.entry_premium
    if pct <= PREMIUM_STOP_PCT:
        return RiskTrigger("premium_stop", RED, f"premium down {pct:.0%} from entry (threshold {PREMIUM_STOP_PCT:.0%})")
    if pct <= PREMIUM_STOP_PCT * YELLOW_PROXIMITY:
        return RiskTrigger("premium_stop", YELLOW, f"premium down {pct:.0%} from entry, approaching {PREMIUM_STOP_PCT:.0%}")
    return None


def _take_profit_trigger(position: OptionPosition, snapshot: MarketSnapshot) -> RiskTrigger | None:
    if snapshot.current_premium is None or position.entry_premium <= 0:
        return None
    pct = (snapshot.current_premium - position.entry_premium) / position.entry_premium
    if pct >= TAKE_PROFIT_PCT:
        return RiskTrigger("take_profit", RED, f"premium up {pct:.0%} from entry (target {TAKE_PROFIT_PCT:.0%})")
    if pct >= TAKE_PROFIT_PCT * YELLOW_PROXIMITY:
        return RiskTrigger("take_profit", YELLOW, f"premium up {pct:.0%} from entry, approaching {TAKE_PROFIT_PCT:.0%}")
    return None


def _dynamic_ev_trigger(snapshot: MarketSnapshot) -> RiskTrigger | None:
    if snapshot.ev_of_continuing is None:
        return None
    if snapshot.ev_of_continuing < 0:
        return RiskTrigger(
            "dynamic_ev_take_profit", RED,
            f"expected value of continuing to hold is negative ({snapshot.ev_of_continuing:.4f}); closing now has higher EV",
        )
    return None


def _seller_early_close_trigger(position: OptionPosition, snapshot: MarketSnapshot) -> RiskTrigger | None:
    if snapshot.current_premium is None or position.entry_premium <= 0:
        return None
    capture = (position.entry_premium - snapshot.current_premium) / position.entry_premium
    if capture >= SELLER_MAX_CAPTURE:
        return RiskTrigger(
            "seller_early_close", RED,
            f"{capture:.0%} of max profit captured (>= {SELLER_MAX_CAPTURE:.0%}); close now to avoid pin risk",
        )
    if capture >= SELLER_MIN_CAPTURE:
        return RiskTrigger(
            "seller_early_close", YELLOW,
            f"{capture:.0%} of max profit captured, inside the {SELLER_MIN_CAPTURE:.0%}-{SELLER_MAX_CAPTURE:.0%} early-close window",
        )
    return None


def _regime_invalidation_trigger(position: OptionPosition, snapshot: MarketSnapshot) -> RiskTrigger | None:
    if position.regime_at_entry is None or snapshot.current_regime is None:
        return None
    if position.regime_at_entry != snapshot.current_regime:
        return RiskTrigger(
            "regime_invalidation", RED,
            f"regime reclassified since entry (was {position.regime_at_entry}, now {snapshot.current_regime}); re-evaluate thesis",
        )
    return None


def assess_position(position: OptionPosition, snapshot: MarketSnapshot) -> RiskAssessment:
    triggers: list[RiskTrigger] = []
    notes: list[str] = []

    if position.side == "long":
        for fn in (_price_stop_trigger, _premium_stop_trigger, _take_profit_trigger):
            t = fn(position, snapshot)
            if t is not None:
                triggers.append(t)
        ev_t = _dynamic_ev_trigger(snapshot)
        if ev_t is not None:
            triggers.append(ev_t)

        if position.entry_iv is None:
            notes.append("price_stop not evaluated: entry_iv missing")
        if snapshot.current_premium is None:
            notes.append("premium_stop/take_profit not evaluated: current_premium missing")
        if snapshot.ev_of_continuing is None:
            notes.append("dynamic_ev_take_profit not evaluated: ev_of_continuing missing (needs production module B serving)")
    else:  # short
        t = _seller_early_close_trigger(position, snapshot)
        if t is not None:
            triggers.append(t)
        if snapshot.current_premium is None:
            notes.append("seller_early_close not evaluated: current_premium missing")

    regime_t = _regime_invalidation_trigger(position, snapshot)
    if regime_t is not None:
        triggers.append(regime_t)
    if position.regime_at_entry is None or snapshot.current_regime is None:
        notes.append("regime_invalidation not evaluated: regime data missing (needs production module A serving)")

    overall = GREEN
    for t in triggers:
        if _LIGHT_RANK[t.light] > _LIGHT_RANK[overall]:
            overall = t.light

    return RiskAssessment(
        position_id=position.position_id,
        as_of_date=snapshot.as_of_date,
        light=overall,
        triggers=triggers,
        notes=notes,
    )
