"""Derived shadow filters V03 / V05 / V06 (EVOLUTION_PLAN.md section 3, P1
table; owner decision 2026-11-10, EVOLUTION_PLAN section 6 item 8). Research
only, read-only: nothing here writes to any table, and nothing in risk,
execution, the options engine or bridge policy imports this module -- a
report never opens, blocks or sizes anything.

Each filter narrows the SAME event scope the 5.1 setup gate uses
(learn/setup_gate.scope_rows / event_pnl), so the three filters and the main
gate agree on what one "event" is; they only differ on which of those events
count, and (V06) how big each one counts as:

  V03 v03_cutoff_highvol: entry before 10:00 ET AND the underlying is not
    high_vol (options/config.OptionsConfig.underlying_class via the
    options_positions.uclass column already recorded at entry). E02/E03's
    control group.
  V05 v05_spread_cap_5pct: entry quote (ask - bid) / mid <= 5% (the live cap
    is 8%, options/config.max_spread_pct). Tighter liquidity filter.
  V06 v06_10k_scale: entry_price * contract_multiplier <= $250 (E04's
    proposed 10k-account premium cap, section 2.5). Composition changes when
    this binds, so its P&L is rescaled to 1 contract (pnl / qty_initial)
    rather than reported at whatever qty the 50k-sleeve sizing actually
    bought -- this is the point of the variant: look at what the capped
    size would have made, not what the current size made.

All three are FORWARD-ONLY: variants.registered_since(name) excludes rows
dated before 2026-11-10, exactly like setup_gate.scope_rows does for
playbook sources. There is no backfill; a variant's sample starts the day it
was registered in learn/variants.py, never before.

Multiple comparisons: these three are registered in the SAME family as every
other variant (learn/variants.py REGISTRY), so learn/setup_gate.rows() and
report() already count them in K and the Bonferroni/Holm-adjusted alpha; this
module reuses the identical k so a V03/V05/V06 verdict is checked at the same
adjusted threshold as the main gate, never a looser one.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from stockmoney.desk.learn import setup_gate as sg
from stockmoney.desk.learn import variants
from stockmoney.desk.options.config import OptionsConfig

ET = ZoneInfo("America/New_York")
CUTOFF_ET = (10, 0)
SPREAD_CAP = 0.05
SCALE_CAP_USD = 250.0

NAMES = ("v03_cutoff_highvol", "v05_spread_cap_5pct", "v06_10k_scale")


def _entry_et(r) -> datetime | None:
    """``r['entry_time']`` (stored ISO, tz-aware) as an ET datetime, or None
    when missing or unparsable: such a row never qualifies for a filter."""
    t = r["entry_time"]
    if not t:
        return None
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(ET)


def _after_registration(r, name: str) -> bool:
    since = variants.registered_since(name)
    return bool(since) and (r["day"] or "") >= since


def _qualifies_v03(r) -> bool:
    if not _after_registration(r, "v03_cutoff_highvol"):
        return False
    et = _entry_et(r)
    if et is None:
        return False
    if (et.hour, et.minute) >= CUTOFF_ET:
        return False
    return r["uclass"] != "high_vol"


def _qualifies_v05(r) -> bool:
    if not _after_registration(r, "v05_spread_cap_5pct"):
        return False
    bid, ask = r["entry_bid"], r["entry_ask"]
    if not (isinstance(bid, (int, float)) and isinstance(ask, (int, float))):
        return False
    mid = (bid + ask) / 2
    if mid <= 0:
        return False
    return (ask - bid) / mid <= SPREAD_CAP


def _qualifies_v06(r, cfg: OptionsConfig) -> bool:
    if not _after_registration(r, "v06_10k_scale"):
        return False
    px = r["entry_price"]
    if not isinstance(px, (int, float)):
        return False
    return px * cfg.contract_multiplier <= SCALE_CAP_USD


def _trade_v06(r, cfg: OptionsConfig) -> dict:
    """event_pnl's trade dict, with pnl rescaled to 1 contract (the point of
    the 10k-scale variant: today's 50k-sleeve qty_initial is not 1)."""
    t = sg.event_pnl(r, cfg)
    qty = r["qty_initial"] or 0
    if qty:
        t = {**t, "pnl": t["pnl"] / qty}
    return t


def rows(con, cfg: OptionsConfig | None = None) -> list[dict]:
    """One dict per filter (v03/v05/v06) with the same metrics fields as
    learn/setup_gate.rows(), at the same Bonferroni/Holm-adjusted alpha
    (shared family size k = variants.family_size())."""
    cfg = cfg or OptionsConfig()
    scope = sg.scope_rows(con, cfg)
    k = variants.family_size()
    groups = {
        "v03_cutoff_highvol": [sg.event_pnl(r, cfg) for r in scope if _qualifies_v03(r)],
        "v05_spread_cap_5pct": [sg.event_pnl(r, cfg) for r in scope if _qualifies_v05(r)],
        "v06_10k_scale": [_trade_v06(r, cfg) for r in scope if _qualifies_v06(r, cfg)],
    }
    return [{"name": name, **sg.metrics(ts, k=k)} for name, ts in groups.items()]


def _f(x, fmt="{:+.0f}") -> str:
    return "n/a" if x is None else fmt.format(x)


def report(con) -> str:
    rs = rows(con)
    k = variants.family_size()
    lines = ["## P1 影子變體(V03/V05/V06,唯讀,owner 2026-11-10)", "",
             "前向影子,登記日之前的資料不算樣本;與 learn/setup_gate 共用事件範圍與家族大小 "
             f"K = {k},門檻同樣用 Bonferroni/Holm 校正後的 alpha。V06 的損益已換算為 1 口。", "",
             "| 變體 | n | 天數 | live/影子 | 平均 | 下界(逐筆/逐日) | 總損益 | 單日占比 | 判定 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in rs:
        lines.append(f"| {r['name']} | {r['n']} | {r['days']} | {r['live']}/{r['shadow']} | {_f(r['mean'])} "
                     f"| {_f(r['lb'])} ({_f(r['lb_trade'])}/{_f(r['lb_day'])}) | {_f(r['total'])} "
                     f"| {_f(r['day_share'], '{:.0%}')} | {r['verdict']} |")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    import argparse
    import sys
    from pathlib import Path

    ap = argparse.ArgumentParser(prog="v_variants", description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=str(sg.DB_PATH))
    a = ap.parse_args(argv[1:])
    con = sqlite3.connect(f"file:{Path(a.db).resolve()}?mode=ro", uri=True)
    try:
        print(report(con))
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv))
