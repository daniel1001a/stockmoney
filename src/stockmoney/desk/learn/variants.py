"""Variant registry (EDGE_PLAN 7.4 item 5, multiple-comparison discipline).

Read-only reporting. Every shadow or live variant that is being measured is
registered here, in code, before its first recorded sample: name, kind,
the date it started counting, trigger, exit, cost assumption and the gate it
must pass. Data recorded before a variant's registered date is not its
sample (learn/setup_gate.py drops those trades). The more variants run at
once, the more of them pass a one-sided 90% lower bound by luck (about
K x 5% of K variants with no edge), so a PASS must still be checked against
the day-share rule and the out-of-sample segment.

Nothing in risk, veto, execution, broker or the options engine imports this
module: a registration never opens, blocks or sizes anything.

How each registered date was found:
  - gap_go, gap_fade, orb15, vwap_reclaim, grok_plan, exit_A:
    git log -S on options/config.py; the four rule playbooks and GROK are in
    5b7168a (2026-10-12) and exit_A in 8ef25a4 (2026-10-12).
    First recorded rows in options_positions are 2026-10-14 (vwap_reclaim)
    and 2026-10-15 (the others), after registration.
  - orb15_5m: 06d5adb (2026-10-27, stop-the-bleeding audit); first row 2026-10-28.
  - orb5_rvol: 04f1a0d (2026-11-05, owner approved 7.4 item 4).
  - stressed_sizing: ad1b87a (2026-11-05, OPTIONS_PLAN 16).
  - mid_entry: registered 2026-11-05 per EDGE_PLAN 7.4 item 2 and the owner's
    2026-11-05 decision; built separately, only registered here.
  - v03_cutoff_highvol, v05_spread_cap_5pct, v06_10k_scale: registered
    2026-11-10 (EVOLUTION_PLAN.md section 6 item 8, owner decision, grok's
    narrower cut of the five P1 candidates). Derived, forward-only shadow
    filters on columns options already records (learn/v_variants.py); no new
    DB table or engine hook. V01/V02/V04 need data the project does not have
    yet (minute-bar replay, point-in-time trend columns, R01) and are
    documented only in EVOLUTION_PLAN.md, not registered here.

Retired, removed from the registry (owner decision 2026-11-10, EVOLUTION_PLAN
section 6 items 5 and 6; family size K goes down, freeing family slots):
  - exit_B (registered 2026-10-12, removed 2026-11-10): no new samples; A was
    better per the plan (+11.1k vs +1.5k over 236 trades each; plan figure, not re-verified in the 2026-11-10 review) and B was never a
    gate arm of its own (7.4 item 6 monthly exit check only). Historical B
    rows stay in options_positions and keep being compared there; they are
    simply no longer part of the live family size.
  - vwap_reclaim_spread (registered 2026-11-05, removed 2026-11-10): both
    arms negative (0dte 12 trades -2,214; 1-3dte 17 trades -2,345);
    options/spread_variant.py's SOURCES is emptied so no new rows are
    created, but options_spread_variants keeps its historical rows and
    learn/setup_gate.report() still prints them (_spread_lines), independent
    of this registry.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

KINDS = ("playbook source", "exit variant", "sizing", "spread", "entry", "filter")


@dataclass(frozen=True)
class Variant:
    name: str
    kind: str
    registered: str  # ISO date it started counting
    trigger: str
    exit: str
    cost_assumption: str
    gate: str


_FILL = "buy at the ask, sell at the bid, fees 0.03 per contract per leg"
_GATE = "5.1 setup gate"

REGISTRY: tuple[Variant, ...] = (
    Variant("gap_go", "playbook source", "2026-10-12", "opening gap continues past the gap level (OPTIONS_PLAN 5, options/playbooks.py)",
            "live exit variant (A)", _FILL, _GATE),
    Variant("gap_fade", "playbook source", "2026-10-12", "opening gap fails back through the gap level (OPTIONS_PLAN 5, options/playbooks.py)",
            "live exit variant (A)", _FILL, _GATE),
    Variant("orb15", "playbook source", "2026-10-12", "break of the 15-minute opening range (OPTIONS_PLAN 5, options/playbooks.py)",
            "live exit variant (A)", _FILL, _GATE),
    Variant("vwap_reclaim", "playbook source", "2026-10-12", "underlying reclaims VWAP (OPTIONS_PLAN 5, options/playbooks.py)",
            "live exit variant (A)", _FILL, _GATE),
    Variant("grok_plan", "playbook source", "2026-10-12", "verified Grok OPTIONS_PLAN setup triggers",
            "live exit variant (A)", _FILL, _GATE),
    Variant("orb15_5m", "playbook source", "2026-10-27",
            "orb15 trigger, invalidation judged on 5-minute closes (shadow only)",
            "live exit variant (A)", _FILL, _GATE),
    Variant("orb5_rvol", "playbook source", "2026-11-05",
            "5-minute opening range break on the top relative-volume names (shadow only)",
            "stop 10% of daily ATR beyond the range level", _FILL, _GATE),
    Variant("exit_A", "exit variant", "2026-10-12", "every entry",
            "premium stop -35%, +40% sells half, rest stops at cost; invalidation, time stop, force flat",
            _FILL, "5.1 setup gate (live exit)"),
    Variant("stressed_sizing", "sizing", "2026-11-05", "every live-exit entry",
            "same as the entry's exit variant",
            "premium stop at its level + half the entry spread + fees both legs; qty = floor($1,000 / cost)",
            "5.1 setup gate (report column)"),
    Variant("mid_entry", "entry", "2026-11-05", "every entry (shadow comparison)",
            "same as the entry's exit variant",
            "limit at the mid, fall back to the ask after a wait; fill rate from recorded quotes",
            "30+ filled comparisons per underlying class (owner 2026-11-05)"),
    Variant("v03_cutoff_highvol", "filter", "2026-11-10",
            "entry_time < 10:00 ET and uclass != 'high_vol' (existing column, forward-only, no backfill)",
            "same as the entry's exit variant (live_exit)", _FILL,
            "5.1 setup gate (derived report, learn/v_variants.py)"),
    Variant("v05_spread_cap_5pct", "filter", "2026-11-10",
            "entry quote (ask-bid)/mid <= 5% (current cap is 8%, forward-only)",
            "same as the entry's exit variant (live_exit)", _FILL,
            "5.1 setup gate (derived report, learn/v_variants.py)"),
    Variant("v06_10k_scale", "filter", "2026-11-10",
            "entry_price * contract_multiplier <= $250 (shadow version of E04's 10k-scale premium cap); "
            "P&L rescaled to 1 contract",
            "same as the entry's exit variant (live_exit)", _FILL,
            "5.1 setup gate (derived report, learn/v_variants.py)"),
)

_BY_NAME = {v.name: v for v in REGISTRY}


def registered_since(name: str) -> str | None:
    """ISO date ``name`` started counting, or None if it is not registered."""
    v = _BY_NAME.get(name)
    return v.registered if v else None


def family_size(as_of: str | date | None = None) -> int:
    """Number of variants registered on or before ``as_of`` (default: all)."""
    if as_of is None:
        return len(REGISTRY)
    d = as_of.isoformat() if isinstance(as_of, date) else str(as_of)[:10]
    return sum(v.registered <= d for v in REGISTRY)


def report(as_of: str | date | None = None) -> str:
    k = family_size(as_of)
    lines = ["## 變體登記表(多重比較紀律,唯讀)", "",
             f"同時登記中的變體:K = {k}。沒有 edge 的變體也有約 5% 會碰巧通過 90% 單邊下界,"
             f"K 個變體約有 {k * 0.05:.1f} 個會靠運氣 PASS;PASS 之後還要看單日占比與樣本外段"
             "(最後 1/3 交易日)平均是否仍為正。登記日之前的資料不算該變體的樣本。", "",
             "| 名稱 | 類別 | 登記日 | 觸發 | 出場 | 成本假設 | 閘門 |", "|---|---|---|---|---|---|---|"]
    lines += [f"| {v.name} | {v.kind} | {v.registered} | {v.trigger} | {v.exit} | {v.cost_assumption} | {v.gate} |"
              for v in REGISTRY]
    return "\n".join(lines)
