"""Bounded data-only entry filters. Missing observations never fire a filter.

No expression evaluation, code loading, sizing increases, or order operations.
"""
from __future__ import annotations

import json
import logging
import math
import operator
import re
from dataclasses import dataclass
from datetime import date
from numbers import Real

from stockmoney.desk.universe import STOCKS

log = logging.getLogger(__name__)
FIELDS = {
    "days_to_earnings": float, "wind": str, "regime": str, "atr_pct": float,
    "rs_rank": float, "vol_ratio": float, "gap_pct": float, "sector": str,
    "vix": float, "spy_above_200d": bool, "grok_verdict": str,
    "minutes_since_open": float, "iv_rank": float,
}
OPS = {"==": operator.eq, "!=": operator.ne, "<": operator.lt, "<=": operator.le,
       ">": operator.gt, ">=": operator.ge, "in": lambda a, b: a in b, "not_in": lambda a, b: a not in b}


def _typed(value, kind) -> bool:
    if kind is float:
        return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)
    return type(value) is kind


def _tree(node, depth=1) -> list[str]:
    if not isinstance(node, dict) or depth > 3:
        raise ValueError("filter condition must be an object with depth <= 3")
    if len(node) == 1 and next(iter(node)) in ("all", "any", "not"):
        op = next(iter(node))
        children = [node[op]] if op == "not" else node[op]
        if not isinstance(children, list) or not 1 <= len(children) <= 6:
            raise ValueError("filter combinator needs 1 to 6 children")
        names = [name for child in children for name in _tree(child, depth + 1)]
        if len(names) > 6:
            raise ValueError("filter supports at most 6 leaves")
        return names
    if set(node) != {"field", "op", "value"}:
        raise ValueError("invalid filter leaf")
    name, op, value = node["field"], node["op"], node["value"]
    if not isinstance(name, str) or name not in FIELDS or not isinstance(op, str) or op not in OPS:
        raise ValueError("unknown filter field or operator")
    kind = FIELDS[name]
    if op in ("<", "<=", ">", ">=") and kind is not float:
        raise ValueError("ordering requires a numeric field")
    values = value if op in ("in", "not_in") else [value]
    if not isinstance(values, list) or not 1 <= len(values) <= 30 or not all(_typed(v, kind) for v in values):
        raise ValueError("filter value has the wrong type")
    return [name]


def validate(rule: dict) -> dict:
    if not isinstance(rule, dict) or set(rule) != {"name", "sleeve", "strategies", "when", "action"}:
        raise ValueError("invalid filter structure")
    if not isinstance(rule["name"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", rule["name"]):
        raise ValueError("invalid filter name")
    if rule["sleeve"] not in ("stocks", "options", "spreads"):
        raise ValueError("invalid filter sleeve")
    strategies = rule["strategies"]
    if not isinstance(strategies, list) or not all(isinstance(s, str) and s for s in strategies):
        raise ValueError("strategies must be a list of names (empty means all)")
    _tree(rule["when"])
    action = rule["action"]
    if not isinstance(action, str):
        raise ValueError("invalid filter action")
    if action != "skip":
        if not re.fullmatch(r"shrink:(?:0?\.\d+)", action) or not 0.25 <= float(action.split(":")[1]) <= 0.75:
            raise ValueError("only skip or shrink:[0.25,0.75] is allowed")
    return rule


def _matches(node, context) -> bool:
    if "all" in node:
        return all(_matches(n, context) for n in node["all"])
    if "any" in node:
        return any(_matches(n, context) for n in node["any"])
    if "not" in node:
        return not _matches(node["not"], context)
    return OPS[node["op"]](context[node["field"]], node["value"])


def evaluate(rule: dict, context: dict) -> tuple[bool, float]:
    validate(rule)
    # Check every referenced field first, including under NOT or an ANY branch.
    # Unknown does not become true through negation or another known branch.
    if any(not _typed(context.get(name), FIELDS[name]) for name in _tree(rule["when"])):
        return False, 1.0
    if not _matches(rule["when"], context):
        return False, 1.0
    return (True, 1.0) if rule["action"] == "skip" else (False, float(rule["action"].split(":")[1]))


@dataclass(frozen=True)
class Decision:
    skip: bool = False
    size_factor: float = 1.0
    names: tuple[str, ...] = ()


def _has_table(con, name) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def active(con, sleeve: str, strategy: str, context: dict, *, paper: bool) -> Decision:
    if not paper or not _has_table(con, "rule_filters"):
        return Decision()
    skip, factor, names = False, 1.0, []
    for row in con.execute("SELECT id, json FROM rule_filters WHERE status='active' ORDER BY id"):
        try:
            rule = validate(json.loads(row[1]))
            if rule["sleeve"] != sleeve or (rule["strategies"] and strategy not in rule["strategies"]):
                continue
            blocked, scale = evaluate(rule, context)
        except (ValueError, TypeError, KeyError):
            log.warning("invalid rule filter id=%s ignored", row[0])
            continue
        if blocked or scale < 1:
            names.append(rule["name"])
            log.info("rule filter %s: %s", rule["name"], rule["action"])
        skip |= blocked
        factor *= scale
    return Decision(skip, factor, tuple(names))


def record_stock_context(con, plan_id, signal, ctx) -> None:
    """Freeze numeric daily observations with the plan, without parsing prose."""
    if not hasattr(ctx, "ind"):
        return  # legacy/manual plans have no indicator observations
    out = {"regime": ctx.market.regime}
    for field, attr in (("vol_ratio", "vol_ratio"), ("atr_pct", "atr20")):
        value = ctx.v(attr, signal.symbol)
        if field == "atr_pct":
            value /= signal.ref_price
        if _typed(value, float):
            out[field] = value
    ranks = ctx.ind.rs63_vs_spy.loc[ctx.t].reindex(list(STOCKS)).dropna().rank(pct=True)
    rank = ranks.get(signal.symbol)
    if _typed(rank, float):
        out["rs_rank"] = float(rank)
    close, sma = ctx.v("close", "SPY"), ctx.v("sma200", "SPY")
    if _typed(close, float) and _typed(sma, float):
        out["spy_above_200d"] = bool(close > sma)
    con.execute("INSERT INTO rule_filter_contexts VALUES (?,?) ON CONFLICT(plan_id) DO UPDATE SET json=excluded.json",
                (plan_id, json.dumps(out)))


def context(con, symbol: str, now, plan_id: str = "") -> dict:
    """Only locally observed fields; absent data stays unknown, never zero."""
    day = now.date().isoformat()
    out = {}
    if plan_id and _has_table(con, "rule_filter_contexts"):
        row = con.execute("SELECT json FROM rule_filter_contexts WHERE plan_id=?", (plan_id,)).fetchone()
        if row:
            out = json.loads(row[0])
    out.update(sector=STOCKS.get(symbol), minutes_since_open=now.hour * 60 + now.minute - 570)
    if _has_table(con, "wind_daily"):
        row = con.execute("SELECT state,regime FROM wind_daily WHERE date<? ORDER BY date DESC LIMIT 1", (day,)).fetchone()
        if row:
            out.update(wind=row[0], regime=row[1])
    if _has_table(con, "earnings"):
        # first_seen is compared as an instant, not lexically across offsets.
        row = con.execute("SELECT date FROM earnings WHERE symbol=? AND date>=? AND removed_at IS NULL "
                          "AND julianday(first_seen)<=julianday(?) ORDER BY date LIMIT 1",
                          (symbol, day, now.isoformat())).fetchone()
        if row:
            out["days_to_earnings"] = (date.fromisoformat(row[0]) - now.date()).days
    return out
