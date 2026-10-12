"""Daily report of the options sleeve, in Chinese for the owner (and, pasted
after GROK_OPTIONS_PROMPT.md part 3, for Grok's review).

Definitions used in every table (closed positions only):
- 淨損益 = realized P&L at the actual / shadow fills (ask in, bid out) minus fees;
- 報酬率 = 淨損益 / 進場權利金;
- 期望值 = mean 淨損益 per trade (already after spread and fees);
- 價差成本 = half the bid/ask spread at entry plus half at exit, versus the mid;
- 價差佔比 = 價差成本 / |以中價計的毛損益| summed over the group (how much of
  the gross move the spread ate) and 價差/權利金 = 價差成本 / 進場權利金;
- MFE / MAE = best / worst bid seen while open, relative to the entry price.
Shadow records several contract variants per trigger, so its counts are
trades-per-variant, not independent ideas.
"""
from __future__ import annotations

import json
from collections import defaultdict

from stockmoney.desk.options import store as ostore
from stockmoney.desk.options.config import GROK, OptionsConfig

CLASS_LABEL = {"index": "指數/槓桿 ETF", "mega_cap": "大型個股", "high_vol": "高波動股"}
STATE_LABEL = {"WAITING": "等待", "TRIGGERED": "已觸發", "ENTERED": "持倉中", "EXITED": "已出場", "EXPIRED": "未觸發",
               "MISSED": "錯過", "SKIPPED": "觸發但未進場", "SUPERSEDED": "被新劇本取代"}
REASON_LABEL = {"premium_stop": "權利金停損", "take_profit": "停利(先出一半)", "breakeven_stop": "成本價停損",
                "invalidation": "推翻價位", "time_stop": "時間停損", "force_flat": "15:45 強制平倉",
                "session_end": "收盤補記", "entry_unfilled": "進場未成交", "entry_not_sent": "未送出"}


def _metrics(rows: list[dict], mult: int) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0}
    net = [r["realized_usd"] - r["fees_usd"] for r in rows]
    cost = [r["qty_initial"] * r["entry_price"] * mult for r in rows]
    spread = [r["spread_cost_usd"] or 0.0 for r in rows]
    gross_mid = sum(abs(a + s) for a, s in zip(net, spread))
    mfe = [(r["max_bid"] or 0) / r["entry_price"] - 1 for r in rows if r["entry_price"]]
    mae = [(r["min_bid"] or 0) / r["entry_price"] - 1 for r in rows if r["entry_price"]]
    return {
        "n": n, "win": sum(1 for x in net if x > 0) / n, "exp": sum(net) / n, "total": sum(net),
        "ret": sum(a / c for a, c in zip(net, cost) if c) / n,
        "spread_share": sum(spread) / gross_mid if gross_mid else 0.0,
        "spread_prem": sum(spread) / sum(cost) if sum(cost) else 0.0,
        "mfe": sum(mfe) / len(mfe) if mfe else 0.0, "mae": sum(mae) / len(mae) if mae else 0.0,
    }


def _table(groups: dict[str, list[dict]], mult: int, label: str) -> list[str]:
    if not groups:
        return ["(無資料)"]
    out = [f"| {label} | 筆數 | 勝率 | 期望值/筆 | 合計 | 平均報酬率 | 價差佔比 | 價差/權利金 | MFE | MAE |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for k in sorted(groups):
        m = _metrics(groups[k], mult)
        if not m["n"]:
            continue
        out.append(f"| {k} | {m['n']} | {m['win']:.0%} | {m['exp']:+.0f} | {m['total']:+.0f} | {m['ret']:+.1%} | "
                   f"{m['spread_share']:.0%} | {m['spread_prem']:.1%} | {m['mfe']:+.0%} | {m['mae']:+.0%} |")
    return out


def _group(rows: list[dict], key) -> dict[str, list[dict]]:
    g: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        g[key(r)].append(r)
    return g


def build(con, day: str, cfg: OptionsConfig | None = None, header: str = "") -> str:
    cfg = cfg or OptionsConfig()
    ostore.ensure_schema(con)
    mult = cfg.contract_multiplier
    setups = ostore.setups(con, day)
    pos = ostore.positions(con, day)
    closed = [p for p in pos if p["state"] == "EXITED" and p["entry_price"]]
    shadow = [p for p in closed if p["mode"] == "shadow"]
    live_all = [p for p in pos if p["mode"] == "live"]
    live = [p for p in closed if p["mode"] == "live"]
    lines = [f"# {day} 期權短打日報", ""]
    if header:
        lines += [header, ""]

    # ---- overview
    st = defaultdict(int)
    for s in setups:
        st[s["state"]] += 1
    trig = [s for s in setups if s["signal_json"]]
    lines += ["## 總覽", "",
              f"- 劇本實例 {len(setups)} 個,觸發 {len(trig)} 個;狀態:"
              + "、".join(f"{STATE_LABEL.get(k, k)} {v}" for k, v in sorted(st.items())),
              f"- 影子部位 {len([p for p in pos if p['mode'] == 'shadow'])} 筆(每次觸發最多 "
              f"{len(cfg.dte_buckets) * len(cfg.delta_bands)} 個合約變體),已結算 {len(shadow)} 筆,"
              f"合計 {sum(p['realized_usd'] - p['fees_usd'] for p in shadow):+.0f} 美元",
              f"- 紙上實單 {len(live_all)} 筆,已結算 {len(live)} 筆,"
              f"合計 {sum(p['realized_usd'] - p['fees_usd'] for p in live):+.0f} 美元", ""]

    # ---- live
    lines += ["## 紙上實單", ""]
    if live_all:
        lines += ["| 標的 | 劇本 | 合約 | 口數 | 進場 | 出場原因 | 淨損益 | MFE | MAE |", "|---|---|---|---|---|---|---|---|---|"]
        for p in live_all:
            net = (p["realized_usd"] or 0) - (p["fees_usd"] or 0)
            ep = p["entry_price"] or 0
            mfe = f"{(p['max_bid'] or 0) / ep - 1:+.0%}" if ep else "-"
            mae = f"{(p['min_bid'] or 0) / ep - 1:+.0%}" if ep else "-"
            lines.append(f"| {p['underlying']} | {p['source']} | {p['occ']} | {p['qty_initial'] or 0:g} | "
                         f"{ep:.2f} | {REASON_LABEL.get(p['exit_reason'] or '', p['exit_reason'] or p['state'])} | "
                         f"{net:+.0f} | {mfe} | {mae} |")
    else:
        lines.append("今天沒有紙上實單。")
    lines.append("")

    # ---- shadow breakdowns
    lines += ["## 影子帳戶(扣價差與費用後)", "", "### 依劇本", ""] + _table(_group(shadow, lambda r: r["source"]), mult, "劇本")
    lines += ["", "### 依標的類別", ""] + _table(
        _group(shadow, lambda r: CLASS_LABEL.get(r["uclass"], r["uclass"])), mult, "類別")
    lines += ["", "### 依到期(DTE)", ""] + _table(_group(shadow, lambda r: r["dte_label"]), mult, "DTE")
    lines += ["", "### 依 Delta 檔位", ""] + _table(_group(shadow, lambda r: r["band_label"]), mult, "Delta")
    lines += ["", "### 依出場原因", ""] + _table(
        _group(shadow, lambda r: REASON_LABEL.get(r["exit_reason"], r["exit_reason"])), mult, "出場原因")

    # ---- grok vs rules
    lines += ["", "## Grok 劇本 vs 規則劇本", ""]
    lines += _table(_group(closed, lambda r: f"{'Grok' if r['source'] == GROK else '規則'}・"
                                              f"{'影子' if r['mode'] == 'shadow' else '紙上'}"), mult, "來源")
    gsetups = [s for s in setups if s["source"] == GROK]
    if gsetups:
        lines += ["", "| 劇本 | 標的 | 方向 | 觸發 | 狀態 | 影子筆數 | 影子合計 | 紙上資格 |", "|---|---|---|---|---|---|---|---|"]
        for s in gsetups:
            spec = json.loads(s["spec_json"] or "{}").get("item", {})
            sig = json.loads(s["signal_json"]) if s["signal_json"] else {}
            ps = [p for p in shadow if p["setup_id"] == s["setup_id"]]
            lines.append(f"| {s['plan_id']}#{s['plan_index']} | {s['underlying']} | {spec.get('bias', '')} | "
                         f"{(spec.get('trigger') or {}).get('kind', '')} {sig.get('at', '')[11:16]} | "
                         f"{STATE_LABEL.get(s['state'], s['state'])} | {len(ps)} | "
                         f"{sum(p['realized_usd'] - p['fees_usd'] for p in ps):+.0f} | "
                         f"{'是' if s['live_eligible'] else s['live_note']} |")
    else:
        lines += ["", "今天沒有生效的 Grok 劇本。"]

    # ---- shadow vs live, same setup and variant
    pairs = []
    for p in live:
        twin = next((q for q in shadow if q["setup_id"] == p["setup_id"] and q["dte_label"] == p["dte_label"]
                     and q["band_label"] == p["band_label"]), None)
        if twin:
            pairs.append((p, twin))
    if pairs:
        lines += ["", "## 影子 vs 紙上(同一劇本、同一變體)", "",
                  "| 合約 | 紙上進場 | 影子進場 | 紙上淨損益 | 影子淨損益 |", "|---|---|---|---|---|"]
        for p, q in pairs:
            lines.append(f"| {p['occ']} | {p['entry_price']:.2f} | {q['entry_price']:.2f} | "
                         f"{p['realized_usd'] - p['fees_usd']:+.0f} | {q['realized_usd'] - q['fees_usd']:+.0f} |")

    # ---- filtered
    rows = con.execute("SELECT stage, reason, COUNT(*) n FROM options_filtered WHERE day=? GROUP BY stage, reason "
                       "ORDER BY n DESC", (day,)).fetchall()
    lines += ["", "## 被過濾掉的機會", ""]
    if rows:
        lines += ["| 階段 | 原因 | 次數 |", "|---|---|---|"] + [f"| {r['stage']} | {r['reason']} | {r['n']} |" for r in rows]
    else:
        lines.append("(無)")

    # ---- anomalies
    ev = con.execute("SELECT level, kind, COUNT(*) n, MAX(message) m FROM options_events WHERE day=? AND level<>'info' "
                     "GROUP BY level, kind ORDER BY n DESC", (day,)).fetchall()
    lines += ["", "## 異常", ""]
    if ev:
        lines += [f"- [{r['level']}] {r['kind']} x{r['n']}:{(r['m'] or '')[:160]}" for r in ev]
    else:
        lines.append("(無)")
    return "\n".join(lines) + "\n"
