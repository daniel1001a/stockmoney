"""Every tunable of the intraday options sleeve, in one place.

Defaults come from docs/desk/OPTIONS_PLAN.md (sections 3 to 5). They are v1
starting points, not fitted values: changing one goes through the parameter
change process in OPERATING_PLAN.md section 6, never silently. Budget-level
risk limits (premium per trade, max positions, daily loss, drawdown) are
enforced by line A's OptionsRiskEngine; the copies here only size orders and
must never be looser than line A's.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

INDEX = ("SPY", "QQQ", "TQQQ")
MEGA_CAP = ("NVDA", "TSLA", "AMD", "AAPL", "MSFT", "META", "AMZN", "GOOGL", "AVGO", "PLTR", "MU", "NFLX")
HIGH_VOL = ("COIN", "SMCI")

RULE_PLAYBOOKS = ("gap_go", "gap_fade", "orb15", "vwap_reclaim")
GROK = "grok_plan"


@dataclass(frozen=True)
class DeltaBand:
    lo: float
    hi: float

    @property
    def label(self) -> str:
        return f"d{round(self.lo * 100)}-{round(self.hi * 100)}"

    @property
    def center(self) -> float:
        return (self.lo + self.hi) / 2


@dataclass(frozen=True)
class DteBucket:
    """Expiry bucket in TRADING days from the session date (0 = today)."""
    label: str
    min_days: int
    max_days: int


@dataclass(frozen=True)
class OptionsConfig:
    # --- budget (section 3); line A's risk engine is the binding check
    capital_budget: float = 50_000.0
    sleeve_pct: float = 0.10
    max_premium_per_trade: float = 1_000.0
    max_open_positions: int = 3
    max_live_per_underlying: int = 1          # engine-side: no stacking two setups on one name
    contract_multiplier: int = 100

    # --- clock (ET, (hour, minute))
    session_start: tuple[int, int] = (9, 20)
    session_end: tuple[int, int] = (16, 0)
    plan_cutoff: tuple[int, int] = (9, 20)    # plans received after this are shadow-only
    open_time: tuple[int, int] = (9, 30)
    entry_window_start: tuple[int, int] = (9, 30)
    entry_window_end: tuple[int, int] = (11, 30)
    force_flat: tuple[int, int] = (15, 45)
    poll_seconds: float = 20.0               # section 8: 15 to 30 s

    # --- exits (section 5, fixed, not tuned yet)
    premium_stop_pct: float = 0.35
    take_profit_pct: float = 0.40
    take_profit_fraction: float = 0.5
    time_stop_minutes: int = 180

    # --- orders
    entry_limit_offset: float = 0.0          # added to the ask for the entry limit
    entry_timeout_s: float = 20.0            # cancel an unfilled entry after this
    exit_timeout_s: float = 20.0             # reprice an unfilled exit after this
    exit_max_limit_attempts: int = 3         # then a market sell

    # --- data safety
    max_quote_age_s: float = 30.0            # never enter on an older option quote
    max_bar_age_s: float = 180.0             # last completed underlying bar must be this fresh
    max_signal_age_s: float = 120.0          # a trigger seen later than this (e.g. after a restart) is missed, not chased
    max_consecutive_errors: int = 5          # then notify (exits keep being retried)

    # --- universe (section 4)
    index: tuple[str, ...] = INDEX
    mega_cap: tuple[str, ...] = MEGA_CAP
    high_vol: tuple[str, ...] = HIGH_VOL
    scan_candidates: tuple[str, ...] = ()    # extra tickers the gap / relative volume scan may add
    scan_min_gap_pct: float = 0.03
    scan_min_premarket_dollar_vol: float = 20_000_000.0
    daily_universe_cap: int = 25
    underlying_max_spread_pct: float = 0.002  # stock bid/ask spread / mid

    # --- contract liquidity (section 3)
    max_spread_pct: float = 0.10             # option (ask - bid) / mid
    min_mid: float = 0.20
    min_open_interest: int = 100             # checked only when the provider reports OI
    min_volume: int = 10                     # contract day volume so far

    # --- contract variants (section 5): shadow records all, live uses one
    dte_buckets: tuple[DteBucket, ...] = (DteBucket("0dte", 0, 0), DteBucket("1-3dte", 1, 3))
    delta_bands: tuple[DeltaBand, ...] = (DeltaBand(0.30, 0.40), DeltaBand(0.40, 0.50), DeltaBand(0.50, 0.60))
    live_dte: str = "0dte"                   # Grok's preferred_dte overrides when not "any"
    live_delta_band: str = "d40-50"
    default_iv: float = 0.40                 # only for the synthetic replay model

    # --- playbooks (section 5)
    gap_min_pct_index: float = 0.004
    gap_min_pct_stock: float = 0.010
    premarket_min_dollar_vol: float = 5_000_000.0  # proxy for "premarket volume"
    gap_go_range_minutes: int = 5
    orb_minutes: int = 15
    orb_volume_lookback: int = 5
    vwap_extension_pct_index: float = 0.003
    vwap_extension_pct_stock: float = 0.008
    vwap_move_window_minutes: int = 30
    vwap_hold_bars: int = 2

    # --- shadow fill model
    shadow_slippage_pct: float = 0.0         # extra, on top of buying the ask / selling the bid
    fee_per_contract: float = 0.03           # regulatory fees; Alpaca charges no commission

    # --- live eligibility: only these sources may send a (paper) order
    live_sources: tuple[str, ...] = RULE_PLAYBOOKS + (GROK,)
    paper_only: bool = True

    # --- paths
    halt_file: Path = Path("data/desk/HALT")
    lock_path: Path = Path("data/desk/options.lock")
    report_dir: Path = Path("data/desk/options/reports")
    replay_cache: Path = Path("data/desk/options/replay_cache")

    @property
    def sleeve_budget(self) -> float:
        return self.capital_budget * self.sleeve_pct

    def underlying_class(self, symbol: str) -> str:
        """index | mega_cap | high_vol. Tickers outside the starting list
        (Grok or scan additions) are catalyst names and count as high_vol."""
        if symbol in self.index:
            return "index"
        if symbol in self.mega_cap:
            return "mega_cap"
        return "high_vol"

    def gap_min(self, symbol: str) -> float:
        return self.gap_min_pct_index if symbol in self.index else self.gap_min_pct_stock

    def vwap_extension(self, symbol: str) -> float:
        return self.vwap_extension_pct_index if symbol in self.index else self.vwap_extension_pct_stock

    def starting_universe(self) -> list[str]:
        return list(self.index) + list(self.mega_cap) + list(self.high_vol)
