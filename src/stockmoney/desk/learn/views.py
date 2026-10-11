"""Read-only query helpers behind the console's agent and learning pages.

Kept out of the Streamlit file so they can be tested. Nothing here writes to
the ledger or touches an order; the only write in the console that uses this
package is the owner's one-line lesson on a post-mortem card.
"""
from __future__ import annotations

import json

import pandas as pd

MUSE, GROK = "Muse", "Pika(Grok)"


def agent_of_message(type_: str, msg_id: str) -> str:
    return GROK if type_ == "GROK_DAILY" or str(msg_id).lower().startswith("grok-") else MUSE


def agent_of_reject(subject: str | None, msg_id: str | None) -> str:
    if str(msg_id or "").lower().startswith("grok-") or "GROK_DAILY" in str(subject or ""):
        return GROK
    return MUSE


def summarize(type_: str, parsed_json: str) -> str:
    """One readable line per stored message (free text is shown, never executed)."""
    try:
        m = json.loads(parsed_json)
    except (TypeError, ValueError):
        return ""
    try:
        if type_ == "GROK_DAILY":
            return (f"事件 {len(m['events'])}、事件鏈 {len(m['chains'])}、喊單 {len(m['calls'])}、"
                    f"熱度 {len(m['attention'])}、追蹤帳號 {len(m['tracked_accounts'])}")
        if type_ == "SOCIAL_DAILY":
            return f"{len(m['items'])} 個標的的社群快照"
        if type_ == "CALLS":
            return f"{len(m['calls'])} 則喊單"
        if type_ == "ALERT":
            return f"{m['ticker']} {m['severity']}:{m['headline']}"
        if type_ == "RESEARCH_REPLY":
            return f"{m['ticker']} 風險旗標 {len(m.get('risk_flags', []))} 個,情緒 {m.get('sentiment_shift', '?')}"
        if type_ == "IDEA":
            return f"{m['ticker']} {m['direction']} {m['horizon_days']} 天:{m['thesis'][:80]}"
    except (KeyError, TypeError):
        return ""
    return ""


def messages(con, agent: str, limit: int = 100) -> pd.DataFrame:
    rows = con.execute("SELECT received_at, type, msg_id, parsed_json FROM bridge_messages "
                       "ORDER BY received_at DESC LIMIT 1000").fetchall()
    out = [{"received_at": r["received_at"], "type": r["type"], "msg_id": r["msg_id"],
            "摘要": summarize(r["type"], r["parsed_json"])}
           for r in rows if agent_of_message(r["type"], r["msg_id"]) == agent]
    return pd.DataFrame(out[:limit], columns=["received_at", "type", "msg_id", "摘要"])


def rejects(con, agent: str, limit: int = 50) -> pd.DataFrame:
    rows = con.execute("SELECT received_at, msg_id, reason, subject FROM bridge_rejects "
                       "ORDER BY id DESC LIMIT 500").fetchall()
    out = [{"received_at": r["received_at"], "msg_id": r["msg_id"], "reason": r["reason"]}
           for r in rows if agent_of_reject(r["subject"], r["msg_id"]) == agent]
    return pd.DataFrame(out[:limit], columns=["received_at", "msg_id", "reason"])


def scorecard_by_agent(card: dict) -> dict[str, dict]:
    """Split muse_ops.scorecard deliveries by who owes them."""
    from stockmoney.desk.muse_ops import EXPECTATIONS
    out: dict[str, dict] = {MUSE: {}, GROK: {}}
    for key, v in card["deliveries"].items():
        who = GROK if getattr(EXPECTATIONS.get(key), "who", "Muse") == "Grok" else MUSE
        out[who][key] = v
    return out


def latest_grok(con) -> dict | None:
    r = con.execute("SELECT msg_id, received_at, parsed_json FROM bridge_messages WHERE type='GROK_DAILY' "
                    "ORDER BY received_at DESC LIMIT 1").fetchone()
    if r is None:
        return None
    return {"msg_id": r["msg_id"], "received_at": r["received_at"], "data": json.loads(r["parsed_json"])}


# ------------------------------------------------------------------ learning

BOOK_LABEL = {
    "bench:spy": "SPY 買進持有",
    "bench:ew_hold": "清單等權持有",
    "bench:ew_gate": "清單等權 x 大盤濾網",
    "shadow:combined": "現行規則(影子)",
    "shadow:combined_t35": "寬停損 3.5 ATR",
    "shadow:combined_earn7": "財報前 7 天不進場",
    "shadow:breakout": "只用突破",
    "shadow:pullback": "只用回檔",
    "shadow:rotation": "只用輪動",
}


def shadow_table(con) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(cumulative return % per labelled book incl. random median, summary table)."""
    rows = con.execute("SELECT name, date, equity FROM shadow_equity").fetchall()
    if not rows:
        return pd.DataFrame(), pd.DataFrame()
    eq = pd.DataFrame([dict(r) for r in rows]).pivot(index="date", columns="name", values="equity").sort_index()
    eq.index = pd.to_datetime(eq.index)
    base = eq.iloc[0]
    cum = (eq / base - 1) * 100
    rnd = [c for c in cum.columns if c.startswith("shadow:random_trend_")]
    shown = pd.DataFrame(index=cum.index)
    for k, label in BOOK_LABEL.items():
        if k in cum:
            shown[label] = cum[k]
    if rnd:
        shown["隨機進場中位數(多頭排列)"] = cum[rnd].median(axis=1)
    last = shown.iloc[-1]
    dd = {}
    for c in shown.columns:
        e = (1 + shown[c] / 100)
        dd[c] = float((e / e.cummax() - 1).min() * 100)
    summary = pd.DataFrame({"累計報酬 %": last, "最大回撤 %": pd.Series(dd)}).round(2)
    return shown, summary


def postmortems(con, limit: int = 200) -> pd.DataFrame:
    return pd.DataFrame([dict(r) for r in con.execute(
        "SELECT trade_id, exit_day, symbol, strategy, ret, r_multiple, exit_kind, symptoms, status, lesson "
        "FROM trade_postmortem ORDER BY exit_day DESC, symbol LIMIT ?", (limit,)).fetchall()])


def chain_table(con) -> pd.DataFrame:
    rows = con.execute(
        "SELECT c.chain_id, c.created_at, c.source, c.trigger_text, c.confidence, "
        "(SELECT COUNT(*) FROM chain_scores s WHERE s.chain_id=c.chain_id AND s.declared=1) AS scored, "
        "(SELECT SUM(hit) FROM chain_scores s WHERE s.chain_id=c.chain_id AND s.declared=1) AS hits "
        "FROM chains c ORDER BY c.created_at DESC LIMIT 100").fetchall()
    return pd.DataFrame([dict(r) for r in rows])
