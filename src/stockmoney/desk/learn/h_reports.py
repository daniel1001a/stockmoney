"""Pre-registered hypothesis reports H1 and H2 (docs/desk/EDGE_PLAN.md 2.1(a), 2.5).

  H1  shadow:combined_t35   vs shadow:combined   (wider trailing stop, 3.5 ATR)
  H2  shadow:combined_earn7 vs shadow:combined   (no new entries within 7 days of earnings)

Both compare the same four things: cumulative return, max drawdown, per-trade
expectancy with its 95% lower confidence bound (mean - 1.96 * se, EDGE_PLAN
2.4), and an MFE / MAE summary computed from the bars between entry and exit.

Acceptance (EDGE_PLAN 2.4 / 2.5): both books have at least MIN_TRADES closed
trades, the candidate's lower bound is higher than the baseline's, and the
candidate's max drawdown is not larger. Fewer trades is never a failure, it is
"insufficient sample".

Nothing here changes a rule. The verdict is recorded as a DRAFT row in
``param_changes`` (inserted directly: ``store.change_param`` would also write
the live ``strategy_params`` setting) and waits for the owner. Reads only
shadow accounts and bars; no broker or risk imports.
"""
from __future__ import annotations

import json
import math

import pandas as pd

from stockmoney.desk import store
from stockmoney.desk.data import Bars
from stockmoney.desk.learn import shadow

Z95 = 1.96
MIN_TRADES = 30
BASELINE = "shadow:combined"
HYPOTHESES = {
    "H1": {"candidate": "shadow:combined_t35", "param": "DRAFT:trail_atr", "old": 3.0, "new": 3.5,
           "title": "H1 出場放寬(移動停損 3.0 改 3.5 ATR)"},
    "H2": {"candidate": "shadow:combined_earn7", "param": "DRAFT:earnings_entry_block_days", "old": 0, "new": 7,
           "title": "H2 財報前 7 天不開新倉"},
}
DRAFT_MARK = "DRAFT"
DATA_SEP = " | data="


def trade_stats(trades: list[dict]) -> dict:
    """Per-trade expectancy of ``ret`` with a 95% interval (normal approximation)."""
    rets = [float(t["ret"]) for t in trades if t.get("ret") is not None]
    n = len(rets)
    if n == 0:
        return {"n": 0, "mean": None, "lcb": None, "ucb": None, "win_rate": None}
    mean = sum(rets) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (n - 1)) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 1 else 0.0
    return {"n": n, "mean": mean, "lcb": mean - Z95 * se if n > 1 else None,
            "ucb": mean + Z95 * se if n > 1 else None, "win_rate": sum(r > 0 for r in rets) / n}


def equity_stats(eq: pd.Series) -> dict:
    eq = eq.dropna()
    if len(eq) < 2:
        return {"days": len(eq), "ret": None, "max_dd": None}
    return {"days": len(eq), "ret": float(eq.iloc[-1] / eq.iloc[0] - 1), "max_dd": float((eq / eq.cummax() - 1).min())}


def mfe_mae(trades: list[dict], bars: Bars | None) -> dict:
    """Median max favourable / adverse excursion (vs entry price) over each
    trade's holding span, and median share of the MFE the exit kept. Trades
    without bars for their symbol or dates are skipped; ``n`` says how many
    were measured."""
    if bars is None:
        return {"n": 0, "mfe": None, "mae": None, "capture": None}
    mfes, maes, caps = [], [], []
    for t in trades:
        sym = t.get("symbol")
        if sym not in bars.high.columns or not t.get("entry_price"):
            continue
        span = slice(pd.Timestamp(t["entry_date"]), pd.Timestamp(t["exit_date"]))
        hi, lo = bars.high[sym].loc[span].dropna(), bars.low[sym].loc[span].dropna()
        if hi.empty or lo.empty:
            continue
        mfe, mae = float(hi.max()) / t["entry_price"] - 1, float(lo.min()) / t["entry_price"] - 1
        mfes.append(mfe)
        maes.append(mae)
        if mfe > 0 and t.get("ret") is not None:
            caps.append(float(t["ret"]) / mfe)

    def med(xs):
        return float(pd.Series(xs).median()) if xs else None
    return {"n": len(mfes), "mfe": med(mfes), "mae": med(maes), "capture": med(caps)}


def _book(accounts: dict, eq: pd.DataFrame, name: str, bars: Bars | None) -> dict:
    acct = accounts.get(name, (None,))[0]
    trades = list(acct.trades) if acct is not None else []
    series = eq[name] if name in eq.columns else pd.Series(dtype=float)
    return {"name": name, **equity_stats(series), **{"trades": trade_stats(trades)}, "mfe_mae": mfe_mae(trades, bars)}


def verdict(cand: dict, base: dict) -> tuple[str, str]:
    """('PASS' | 'FAIL' | 'INSUFFICIENT', one-line reason)."""
    nc, nb = cand["trades"]["n"], base["trades"]["n"]
    if min(nc, nb) < MIN_TRADES:
        return "INSUFFICIENT", f"樣本不足(候選 {nc} 筆、基準 {nb} 筆,各需至少 {MIN_TRADES} 筆),不下結論"
    lc, lb = cand["trades"]["lcb"], base["trades"]["lcb"]
    dc, db = cand["max_dd"], base["max_dd"]
    if lc is None or lb is None or dc is None or db is None:
        return "INSUFFICIENT", "期望值下界或最大回撤無法計算,不下結論"
    lcb_up, dd_ok = lc > lb, dc >= db  # drawdowns are negative: >= means not larger
    if lcb_up and dd_ok:
        return "PASS", f"期望值下界 {lc:+.2%} 高於基準 {lb:+.2%},最大回撤 {dc:.2%} 沒有比基準 {db:.2%} 大"
    why = []
    if not lcb_up:
        why.append(f"期望值下界 {lc:+.2%} 沒有高於基準 {lb:+.2%}")
    if not dd_ok:
        why.append(f"最大回撤 {dc:.2%} 比基準 {db:.2%} 大")
    return "FAIL", ";".join(why)


def compare(con, hyp: str, bars: Bars | None = None) -> dict:
    spec = HYPOTHESES[hyp]
    accounts, eq = store.load_accounts(con), shadow.equity_table(con)
    cand = _book(accounts, eq, spec["candidate"], bars)
    base = _book(accounts, eq, BASELINE, bars)
    v, why = verdict(cand, base)
    return {"hypothesis": hyp, "verdict": v, "reason": why, "candidate": cand, "baseline": base}


def _pct(x, signed=True) -> str:
    return "n/a" if x is None else (f"{x:+.2%}" if signed else f"{x:.2%}")


def render(res: dict) -> str:
    spec = HYPOTHESES[res["hypothesis"]]
    label = {"PASS": "建議通過(仍須你核准)", "FAIL": "不通過", "INSUFFICIENT": "樣本不足"}[res["verdict"]]
    lines = [f"## {spec['title']}", "", f"結論:{label}。{res['reason']}。", "",
             "| 帳戶 | 天數 | 累計報酬 | 最大回撤 | 筆數 | 每筆期望值 | 95% 下界 | MFE 中位 | MAE 中位 | 利潤保留率 |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for b in (res["baseline"], res["candidate"]):
        t, m = b["trades"], b["mfe_mae"]
        lines.append(f"| {b['name']} | {b['days']} | {_pct(b['ret'])} | {_pct(b['max_dd'], False)} | {t['n']} | "
                     f"{_pct(t['mean'])} | {_pct(t['lcb'])} | {_pct(m['mfe'])} | {_pct(m['mae'])} | "
                     f"{_pct(m['capture'], False)} |")
    lines += ["", f"MFE/MAE 以進出場期間的日高低點計,可量測 {res['baseline']['mfe_mae']['n']} / "
                  f"{res['candidate']['mfe_mae']['n']} 筆;利潤保留率 = 單筆報酬 / MFE 的中位數。",
              f"接受條件(EDGE_PLAN 2.4 / 2.5):兩邊各至少 {MIN_TRADES} 筆已平倉、候選的期望值 95% 下界高於基準、"
              "最大回撤沒有變大。本報告不會改任何規則。"]
    if res["hypothesis"] == "H1":
        lines.append("限制:影子帳戶 t35 只放寬移動停損,沒有包含 EDGE_PLAN 2.1(a) 的「回檔持有上限 40 改 60 天」,"
                     "也沒有隨機對照百分位;這兩項需另行驗證才能完整回答 H1。")
    return "\n".join(lines)


def _signature(res: dict) -> str:
    return f"{res['verdict']}|{res['candidate']['trades']['n']}|{res['baseline']['trades']['n']}"


def record_draft(con, res: dict) -> bool:
    """Insert a DRAFT row into param_changes (no live setting is touched).
    Skipped when the newest draft of this hypothesis has the same signature, so
    a daily run does not pile up identical rows. Returns True when written."""
    spec, hyp = HYPOTHESES[res["hypothesis"]], res["hypothesis"]
    sig = _signature(res)
    last = con.execute("SELECT reason FROM param_changes WHERE strategy=? AND param=? ORDER BY id DESC LIMIT 1",
                       (hyp, spec["param"])).fetchone()
    if last and DATA_SEP in last["reason"]:
        try:
            if json.loads(last["reason"].split(DATA_SEP, 1)[1]).get("sig") == sig:
                return False
        except ValueError:
            pass
    data = {"sig": sig, "status": "needs_owner_approval", "verdict": res["verdict"], "reason": res["reason"],
            "candidate": res["candidate"], "baseline": res["baseline"]}
    con.execute("INSERT INTO param_changes (strategy, param, old, new, reason, changed_at) VALUES (?,?,?,?,?,?)",
                (hyp, spec["param"], json.dumps(spec["old"]), json.dumps(spec["new"]),
                 f"{DRAFT_MARK} {hyp} {res['verdict']}: {res['reason']}{DATA_SEP}{json.dumps(data, default=float)}",
                 store.now()))
    return True


def run(con, hyp: str, bars: Bars | None = None, record: bool = True) -> str:
    """Report text for one hypothesis; records a DRAFT unless ``record`` is False."""
    res = compare(con, hyp, bars)
    text = render(res)
    if record and record_draft(con, res):
        text += "\n\n已寫入 param_changes 草案(DRAFT,待你核准,不會自動生效)。"
    return text


def run_all(con, bars: Bars | None = None, record: bool = True) -> str:
    return "\n\n".join(run(con, h, bars, record) for h in HYPOTHESES)
