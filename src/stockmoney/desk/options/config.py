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
# orb15 benchmark: QQQ for tech / growth names, SPY for everything else
TECH_GROWTH = ("QQQ", "TQQQ", "NVDA", "TSLA", "AMD", "AAPL", "MSFT", "META", "AMZN", "GOOGL", "AVGO", "PLTR", "MU",
               "NFLX", "COIN", "SMCI")


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
    entry_window_start: tuple[int, int] = (9, 35)   # no entry of any kind before 09:35 ET (owner, 2026-10-12)
    entry_window_end: tuple[int, int] = (11, 30)
    force_flat: tuple[int, int] = (15, 45)   # on an early close: close - flat_before_close_min if earlier
    flat_before_close_min: int = 15          # forced flat is never later than the close minus this
    not_flat_alert_min: int = 10             # page (CRITICAL) if not flat this long before the close (15:50 ET)
    min_live_minutes_before_flat: int = 90   # no live entry with less time than this left before forced flat
    poll_seconds: float = 20.0               # section 8: 15 to 30 s
    tick_deadline_s: float = 40.0            # a tick running longer skips scans and shadow work (live first)

    # --- exits (section 5, fixed, not tuned yet). Every entry is recorded
    # once per exit variant; live uses ``live_exit``. Variants are reported
    # separately and never mixed.
    #   A: premium stop -35%; +40% sells half, the rest stops at cost
    #   B: premium stop -30%; +40% sells half, the rest trails the underlying
    #      by trail_atr_mult x ATR(atr_period) of its 1-minute bars
    # Both: underlying invalidation, time stop, flat by force_flat.
    exit_variants: tuple[str, ...] = ("A", "B")
    live_exit: str = "A"
    premium_stop_pct: float = 0.35           # variant A
    premium_stop_pct_b: float = 0.30         # variant B
    take_profit_pct: float = 0.40
    take_profit_fraction: float = 0.5
    trail_atr_mult: float = 0.4              # variant B
    atr_period: int = 14                     # variant B, 1-minute underlying bars
    time_stop_minutes: int = 180

    # --- orders
    entry_limit_offset: float = 0.0          # added to the ask for the entry limit
    entry_timeout_s: float = 20.0            # cancel an unfilled entry after this
    exit_timeout_s: float = 20.0             # reprice an unfilled exit after this
    exit_max_limit_attempts: int = 3         # then a market sell
    cancel_max_failures: int = 3             # failed cancels of one order before the owner is paged
    cancel_page_every_min: float = 5.0       # a working exit that cannot be canceled is re-paged this often
    order_status_max_failures: int = 5       # failed get_order reads of an exit before a fresh market sell
    flatten_budget_s: float = 300.0          # flatten_now stops after this (wall clock) and leaves the rest to finish()
    flat_broker_max_attempts: int = 2        # after the forced flat: attempts per broker call (AlpacaBroker retries)

    # --- data safety
    max_quote_age_s: float = 30.0            # never enter on an older option quote
    max_bar_age_s: float = 180.0             # last completed underlying bar must be this fresh
    max_signal_age_s: float = 120.0          # a trigger seen later than this (e.g. after a restart) is missed, not chased
    max_consecutive_errors: int = 5          # then notify (exits keep being retried)

    # --- request budget (market data). Alpaca's Basic plan (this paper
    # account: IEX stocks, indicative options) allows 200 historical data
    # calls / min for stocks and 200 for options (docs, read 2026-10-12).
    # Low-priority work (scans, shadow quotes) is skipped once this many
    # data calls were made in the last 60 s; live position quotes and every
    # order / exit call are never skipped.
    max_data_calls_per_min: int = 110
    shadow_quote_every_s: float = 30.0       # a shadow-only contract / underlying is re-quoted at most this often
    rate_limit_backoff_s: tuple[float, float] = (10.0, 30.0)  # after an HTTP 429: skip low-priority work, jittered

    # --- universe (section 4)
    index: tuple[str, ...] = INDEX
    mega_cap: tuple[str, ...] = MEGA_CAP
    high_vol: tuple[str, ...] = HIGH_VOL
    scan_candidates: tuple[str, ...] = ()    # extra tickers the gap / relative volume scan may add
    scan_min_gap_pct: float = 0.03
    scan_min_premarket_dollar_vol: float = 20_000_000.0
    daily_universe_cap: int = 25
    prior_symbol_cap: int = 5                # at most this many names a day joined ONLY as a Grok directional prior
    underlying_max_spread_pct: float = 0.002  # stock bid/ask spread / mid

    # --- contract liquidity (section 3); also passed to line A's OptionsLimits
    max_spread_pct: float = 0.08             # option (ask - bid) / mid
    min_mid: float = 0.20
    min_open_interest: int = 100             # checked only when the provider reports OI
    min_volume: int = 10                     # contract day volume so far

    # --- contract variants (section 5): shadow records every DTE bucket x
    # exit variant; live uses live_dte x live_exit. Live never uses a
    # contract that expires today (Alpaca refuses opening orders on it from
    # 15:30 ET, and an ITM option held to expiry is exercised into stock,
    # which would break the stock reconcile). 0DTE is a shadow variant only.
    dte_buckets: tuple[DteBucket, ...] = (DteBucket("0dte", 0, 0), DteBucket("1-3dte", 1, 3))
    live_dte: str = "1-3dte"                 # nearest expiry 1 to 3 trading days out
    delta_band_index: DeltaBand = DeltaBand(0.40, 0.55)
    delta_band_stock: DeltaBand = DeltaBand(0.45, 0.60)
    default_iv: float = 0.40                 # only for the synthetic replay model

    # --- playbooks (section 5, filtered versions, owner 2026-10-12). Volume
    # rules are RATIOS on one feed (the live feed is IEX, a fraction of the
    # tape): never compare an absolute volume.
    # RVOL = volume since the open / mean volume over the same minutes of the
    # previous rvol_lookback sessions (same feed). Fewer than
    # rvol_min_sessions usable sessions = unknown (None): the setup is then
    # shadow-only, never live.
    rvol_lookback: int = 20
    rvol_min_sessions: int = 10
    gap_min_pct_index: float = 0.005
    gap_min_pct_stock: float = 0.015
    gap_go_min_rvol: float = 2.0             # first 5 minutes
    gap_go_range_minutes: int = 5
    gap_go_window: tuple[tuple[int, int], tuple[int, int]] = ((9, 35), (10, 15))
    orb_minutes: int = 15
    orb_volume_lookback: int = 5
    orb_break_volume_mult: float = 1.5       # break bar volume > this x mean of the previous 5 bars
    orb_min_day_rvol: float = 1.5
    orb_window: tuple[tuple[int, int], tuple[int, int]] = ((9, 45), (11, 0))
    orb_risk_atr: tuple[float, float] = (0.5, 0.75)  # initial risk reference, x daily ATR(14): recorded only
    vwap_hold_bars: int = 2
    vwap_slope_bars: int = 15
    vwap_slope_min: float = 0.0005           # |VWAP change over vwap_slope_bars| / VWAP; unfitted starting value
    vwap_window: tuple[tuple[int, int], tuple[int, int]] = ((9, 45), (11, 30))
    gap_fade_window: tuple[tuple[int, int], tuple[int, int]] = ((9, 35), (11, 30))

    # --- Grok plans: live only for these playbooks and with day RVOL at the
    # trigger >= max(grok_min_rvol, the plan's own min_rvol); else shadow only
    grok_live_playbooks: tuple[str, ...] = ("gap_go", "orb")
    grok_min_rvol: float = 1.5

    # --- regime: live entries are blocked within macro_block_minutes before
    # or after a macro event (the plan's market_context.key_events times plus
    # macro_events: ("YYYY-MM-DD", "HH:MM" ET)). Other regime tags are stored
    # per setup, not blocking.
    macro_block_minutes: int = 30
    macro_events: tuple[tuple[str, str], ...] = ()

    # --- shadow fill model
    shadow_slippage_pct: float = 0.0         # extra, on top of buying the ask / selling the bid
    fee_per_contract: float = 0.03           # regulatory fees; Alpaca charges no commission

    # --- live eligibility: only these sources may send a (paper) order;
    # vwap_reclaim and gap_fade are shadow only
    live_sources: tuple[str, ...] = ("gap_go", "orb15", GROK)
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

    def delta_band(self, symbol: str) -> DeltaBand:
        return self.delta_band_index if symbol in self.index else self.delta_band_stock

    def benchmark(self, symbol: str) -> str:
        return "QQQ" if symbol in TECH_GROWTH else "SPY"

    def starting_universe(self) -> list[str]:
        return list(self.index) + list(self.mega_cap) + list(self.high_vol)
