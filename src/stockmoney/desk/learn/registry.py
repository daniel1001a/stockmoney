"""Declared research knobs and audited paper overrides.

Bounds for new options knobs are deliberately conservative, not fitted.
Clock knobs are integer minutes after midnight ET. Risk settings are visible
but never writable here. This module does not approve or apply proposals.
"""
from __future__ import annotations

import json
import math
import logging
from dataclasses import dataclass, fields, replace
from numbers import Real

from stockmoney.desk import parameter_state, profiles, store
from stockmoney.desk.options.config import OptionsConfig
from stockmoney.desk.strategies import REGISTRY as STRATEGIES, Bound


@dataclass(frozen=True)
class Tunable:
    key: str
    sleeve: str
    location: str
    bound: Bound
    max_step: float
    ai_tunable: bool = True


# key -> (config attribute, bound, maximum single proposal step).
# Names follow the existing config, avoiding aliases for the same knob.
OPTION_KNOBS = {
    "gap_min_pct_index": (Bound(0.0025, 0.02), 0.0025),
    "gap_min_pct_stock": (Bound(0.005, 0.05), 0.005),
    "gap_go_min_rvol": (Bound(1, 4), 0.25),
    "orb_break_volume_mult": (Bound(1, 3), 0.25),
    "orb_min_day_rvol": (Bound(1, 3), 0.25),
    "vwap_hold_bars": (Bound(1, 5, integer=True), 1),
    "vwap_slope_bars": (Bound(5, 30, integer=True), 5),
    "vwap_slope_min": (Bound(0.0001, 0.002), 0.0001),
    "grok_min_rvol": (Bound(1, 3), 0.25),
    "entry_window_start": (Bound(575, 600, integer=True), 5),
    "entry_window_end": (Bound(630, 690, integer=True), 15),
}
SPREAD_KNOBS = {
    "take_profit_frac": (Bound(0.25, 0.75), 0.1),
    "stop_frac": (Bound(0.25, 0.75), 0.1),
    "time_exit_dte": (Bound(1, 5, integer=True), 1),
}
CLOCK_KNOBS = {"entry_window_start", "entry_window_end"}


def _build() -> dict[str, Tunable]:
    out = {}
    for name, strategy in STRATEGIES.items():
        for param, bound in strategy.bounds.items():
            key = f"{name}.{param}"
            step = 10 if param == "max_hold_days" else 1 if bound.integer else 0.05 if param == "rs_quantile" else 0.5
            out[key] = Tunable(key, "stocks", "strategy_params", bound, step)
    for sleeve, knobs in (("options", OPTION_KNOBS), ("spreads", SPREAD_KNOBS)):
        for param, (bound, step) in knobs.items():
            key = f"{sleeve}.{param}"
            out[key] = Tunable(key, sleeve, "options_params", bound, step)
    protected = [f"profiles.{f.name}" for f in fields(profiles.Profile) if f.name != "name"]
    protected += ["risk.dd_reduce", "risk.dd_halt", "risk.sleeve_dd_halt", "risk.halt_new_entries",
                  "risk.halt_file", "DESK_OPTIONS_ORDERS", "ALPACA_ENV", "ALPACA_LIVE_ENABLED"]
    protected += [f"options.{p}" for p in (
        "max_premium_per_trade", "sleeve_pct", "max_open_positions", "force_flat",
        "capital_budget", "max_live_per_underlying", "paper_only", "live_sources",
        "flat_before_close_min", "min_live_minutes_before_flat")]
    for key in protected:
        out[key] = Tunable(key, "options" if key.startswith("options.") else "desk", "owner", Bound(), 0, False)
    return out


REGISTRY = _build()
log = logging.getLogger(__name__)


def _settings(con) -> dict:
    # Standalone shadow/replay databases may not have the desk settings table.
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone():
        return {}
    values = json.loads(store.get_setting(con, "options_params", "{}"))
    if not isinstance(values, dict):
        raise ValueError("options overrides must be an object")
    return values


def _attr(key: str) -> str:
    sleeve, param = key.split(".", 1)
    return f"spread_{param}" if sleeve == "spreads" else param


def current(con, key: str):
    item = REGISTRY[key]
    if item.location == "strategy_params":
        strategy, param = key.split(".")
        try:
            params = store.strategy_params(con, paper=True)
        except (ValueError, TypeError):
            params = store.strategy_params(con)
        return params.get(strategy, {}).get(param, STRATEGIES[strategy].params[param])
    if item.location == "options_params":
        default = getattr(OptionsConfig(), _attr(key))
        if key.split(".")[1] in CLOCK_KNOBS:
            default = default[0] * 60 + default[1]
        try:
            values = _checked_settings(con)
        except (ValueError, TypeError):
            values = {}  # same effective values as the exit-only runtime fallback
        return values.get(key, default)
    if key.startswith("profiles."):
        return getattr(profiles.active(), key.split(".")[1])
    if key.startswith("options."):
        return getattr(OptionsConfig(), key.split(".")[1])
    if key == "risk.halt_new_entries":
        return store.get_setting(con, "halt_new_entries", "0")
    if key.startswith("risk."):
        return parameter_state.risk_default(key.split(".")[1])
    return parameter_state.switch_state(key)


def _number(item: Tunable, value):
    try:
        finite = isinstance(value, Real) and math.isfinite(value)
    except OverflowError:
        finite = False
    if (isinstance(value, bool) or not finite
            or not item.bound.contains(value) or (item.bound.integer and value != int(value))):
        raise ValueError(f"{item.key}: expected finite number in {item.bound.describe()}")
    return int(value) if item.bound.integer else float(value)


def validate(con, key: str, value):
    item = REGISTRY.get(key)
    if item is None or not item.ai_tunable:
        raise ValueError(f"{key}: not AI tunable")
    value = _number(item, value)
    if abs(value - current(con, key)) > item.max_step + 1e-12:
        raise ValueError(f"{key}: exceeds max_step {item.max_step}")
    if item.location == "strategy_params":
        strategy, param = key.split(".")
        STRATEGIES[strategy].validate_params({**store.strategy_params(con, paper=True).get(strategy, {}), param: value})
    return value


def change(con, key: str, value, reason: str, *, account_kind: str) -> None:
    """One validated paper change and its audit row, in the caller's transaction."""
    if account_kind != "paper":
        raise ValueError("registry changes are paper-only")
    if not reason.strip():
        raise ValueError("a change reason is required")
    # Acquire the SQLite write lock before reading the current value/document.
    # SAVEPOINT also works inside the caller's existing transaction.
    if not con.in_transaction:
        con.execute("BEGIN IMMEDIATE")
    con.execute("SAVEPOINT registry_change")
    try:
        con.execute("UPDATE settings SET value=value WHERE key='options_params'")
        value = validate(con, key, value)
        item = REGISTRY[key]
        if item.location == "options_params":
            _checked_settings(con)  # refuse a corrupt shared document without an audit/write
        old = current(con, key)
        sleeve, param = key.split(".")
        if item.location == "strategy_params":
            values = json.loads(store.get_setting(con, "paper_strategy_params", "{}"))
            if not isinstance(values, dict) or not isinstance(values.setdefault(sleeve, {}), dict):
                raise ValueError("invalid paper strategy overrides")
            values[sleeve][param] = value
            store.set_setting(con, "paper_strategy_params", json.dumps(values))
        else:
            values = _settings(con)
            values[key] = value
            store.set_setting(con, "options_params", json.dumps(values))
        con.execute("INSERT INTO param_changes (strategy,param,old,new,reason,changed_at) VALUES (?,?,?,?,?,?)",
                    (sleeve, param, json.dumps(old), json.dumps(value), reason, store.now()))
        con.execute("RELEASE registry_change")
    except Exception:
        con.execute("ROLLBACK TO registry_change")
        con.execute("RELEASE registry_change")
        raise


def options_config(con, cfg: OptionsConfig, *, paper: bool, sleeve: str = "options") -> OptionsConfig:
    """Load only declared overrides; a non-paper engine gets its original config."""
    if not paper:
        return cfg
    updates = {}
    for key, value in _checked_settings(con).items():
        item = REGISTRY[key]
        if item.sleeve != sleeve:
            continue
        if key.split(".")[1] in CLOCK_KNOBS:
            value = divmod(value, 60)
        updates[_attr(key)] = value
    return replace(cfg, **updates) if updates else cfg


def _checked_settings(con) -> dict:
    checked = {}
    for key, value in _settings(con).items():
        item = REGISTRY.get(key)
        if item is None or not item.ai_tunable or item.location != "options_params":
            raise ValueError("invalid stored options override")
        checked[key] = _number(item, value)
    return checked


def safe_options_config(con, cfg: OptionsConfig, *, paper: bool, sleeve: str = "options", on_error=None):
    """Invalid research settings must never disable exit/flatten processing."""
    try:
        result = options_config(con, cfg, paper=paper, sleeve=sleeve)
    except (ValueError, TypeError):
        log.error("Invalid paper overrides: new entries disabled; caller config retained for exits")
        if on_error is not None and store.get_setting(con, "registry_override_alert", "0") != "1":
            try:
                on_error("Desk paper overrides invalid", "New entries disabled; code configuration retained for exit handling.")
                store.set_setting(con, "registry_override_alert", "1")
            except Exception:
                log.warning("Could not report invalid paper overrides")
        return cfg
    if paper and con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone():
        if store.get_setting(con, "registry_override_alert", "0") == "1":
            store.set_setting(con, "registry_override_alert", "0")
    return result


def options_settings_valid(con) -> bool:
    try:
        _checked_settings(con)
        return True
    except (ValueError, TypeError):
        return False
