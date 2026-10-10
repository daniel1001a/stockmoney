"""Console actions that change desk configuration, kept out of the Streamlit
script so they can be tested. Each one validates everything first and writes
only if all of it is valid; every change lands in ``param_changes`` with the
owner's reason (CLAUDE.md section 7: nothing is tuned or switched silently)."""
from __future__ import annotations

import json

from stockmoney.desk import store
from stockmoney.desk.strategies import REGISTRY


def _log(con, strategy: str, param: str, old, new, reason: str) -> None:
    con.execute(
        "INSERT INTO param_changes (strategy, param, old, new, reason, changed_at) VALUES (?,?,?,?,?,?)",
        (strategy, param, json.dumps(old), json.dumps(new), reason, store.now()),
    )


def disabled_strategies(con) -> set[str]:
    return set(json.loads(store.get_setting(con, "disabled_strategies", "[]")))


def apply_strategy_changes(con, name: str, edited: dict, enabled: bool, reason: str) -> list[str]:
    """Validate and apply a parameter edit and/or an on/off switch for one
    strategy. Raises ValueError (nothing written) on a bad value, an unknown
    key, or a missing reason. Returns one line per applied change; an empty
    list means nothing differed."""
    if name not in REGISTRY:
        raise ValueError(f"unknown strategy {name!r}")
    cls = REGISTRY[name]
    clean = cls.validate_params(edited)  # raises with the offending parameter named
    stored = store.strategy_params(con).get(name, {})
    current = {**cls.params, **stored}
    changes = {k: v for k, v in clean.items() if v != current[k]}
    was_enabled = name not in disabled_strategies(con)
    switched = enabled != was_enabled
    if not changes and not switched:
        return []
    if not (reason or "").strip():
        raise ValueError("請寫理由")
    reason = reason.strip()
    applied = []
    for k, v in changes.items():
        store.change_param(con, name, k, v, reason)
        applied.append(f"{name}.{k}: {current[k]} -> {v}")
    if switched:
        disabled = disabled_strategies(con)
        disabled = disabled - {name} if enabled else disabled | {name}
        store.set_setting(con, "disabled_strategies", json.dumps(sorted(disabled)))
        _log(con, name, "enabled", was_enabled, enabled, reason)
        applied.append(f"{name}: {'啟用' if enabled else '停用'}")
    return applied


def set_halt(con, halt: bool, reason: str) -> None:
    """Pause (any time) or resume (reason required) new entries. Exits and
    stops are unaffected. Logged in ``param_changes`` under strategy 'desk'.
    A manual halt is recorded as ``halt_source=manual`` and stays until the
    owner lifts it; pausing while an automatic (reconcile) halt is in force
    turns it into a manual one."""
    reason = (reason or "").strip()
    if not halt and not reason:
        raise ValueError("解除暫停必須寫理由")
    old = store.get_setting(con, "halt_new_entries", "0") == "1"
    if old == halt:
        src = store.get_setting(con, "halt_source", "")
        if halt and src != "manual":
            store.set_setting(con, "halt_source", "manual")
            _log(con, "desk", "halt_source", src, "manual", reason or "手動暫停")
        return
    store.set_setting(con, "halt_new_entries", "1" if halt else "0")
    store.set_setting(con, "halt_source", "manual" if halt else "")
    _log(con, "desk", "halt_new_entries", old, halt, reason or "手動暫停")
