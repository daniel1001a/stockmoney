"""Per-source credibility scoreboard: one honest running score per input source.

Read-only analysis. This reads stored bridge messages, the learning tables the
other steps fill (chain_scores, grok_market_risk, blocked_counterfactual,
call_scores), the options sleeve's positions and the daily price cache, and
writes only ``source_scores``. Nothing in veto, risk, bridge policy, the
options engine or execution code reads it: a score never opens, blocks or
sizes anything. A source's authority is reviewed weekly by the owner from this
table (docs/desk/EDGE_PLAN.md); changing it is the owner's decision.

Observations, one per scored claim. ``day`` is the session the claim was made
for (or made on), and the rolling windows select on it:
- grok_in_play: GROK_DAILY.in_play items (optional field; absent in older
  mail). D = the first session whose open came after OUR received_at. Signed
  return = (close_D / open_D - 1) in the stated direction; hit = signed > 0;
  Brier uses ``confidence`` as P(hit) where given. Split confirmed / not.
  Daily bars only: the desk stores no minute bars, so the first-60-minute
  return is not computed (the report says so).
- grok_chains: chain_scores rows of Grok chains at the declared horizon,
  falsifiable chains only (the same evidence rule as chains.summary).
- grok_market_risk: grok_market_risk rows with a 5-session SPY return. Hit =
  elevated/critical followed by a 5-day SPY loss, or low followed by a gain.
- grok_calls / muse_calls: call_scores at 5 sessions (excess vs SPY in the
  call's direction), split by the message type that carried the call.
- muse_alert: every elevated/critical ALERT, one per (ticker, ET day), excess
  vs SPY over 5 sessions from the first complete close after receipt
  (calls.base_session). An ALERT carries no direction; it can only reduce
  risk, so hit = the name then lagged SPY.
- *_block: blocked_counterfactual first blocks of an event with a 5-day result.
  Hit = the blocked name lagged SPY (excess_5 < 0); signed = -excess_5, the
  excess the block avoided. legacy_only rows score the pre-2026-10-21 Muse rule.
- grok_options_plan:<mode>, playbook:<name>:<mode>: exited options positions,
  P&L = realized_usd - fees_usd; exits with unknown P&L are left out (same rule
  as options/report.py). Plan items that never triggered have no position and
  no score.

Windows: the last 20 and 60 sessions of the price cache up to ``through``, and
all-time. Trend compares the 20-session value to the 60-session one and needs
MIN_N observations in the 20-session window, else it reads 樣本不足.
"""
from __future__ import annotations

import json
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from stockmoney.desk import store
from stockmoney.desk.data import Bars
from stockmoney.desk.universe import MARKET

ET = ZoneInfo("America/New_York")
MIN_N = 20
WINDOWS = (("20d", 20), ("60d", 60), ("all", None))
HIT_STEP = 0.05  # hit-rate change (absolute) that counts as a trend
PNL_STEP_USD = 10.0  # per-trade P&L change that counts as a trend (or 10% of the 60d value, if larger)
OPEN_ET = time(9, 30)
UNKNOWN_PNL = ("broker_flat_unreconciled", "manual_resolved")  # options/report.py UNKNOWN_PNL

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_scores (
  day TEXT, source TEXT, metric TEXT, n INTEGER, value REAL, window TEXT, computed_at TEXT,
  PRIMARY KEY (day, source, metric, window));
"""

# always listed, in this order, so a silent source is visible as 0
FIXED = ("grok_in_play", "grok_in_play:confirmed", "grok_in_play:unconfirmed", "grok_chains", "grok_market_risk",
         "grok_calls", "muse_calls", "muse_alert", "grok_alert", "muse_alert_block", "muse_risk_flags_block",
         "research_veto_block", "muse_legacy_rule", "grok_options_plan:live", "grok_options_plan:shadow")
LABEL = {
    "grok_in_play": "Grok in_play(開盤到收盤)", "grok_in_play:confirmed": "Grok in_play 已證實",
    "grok_in_play:unconfirmed": "Grok in_play 未證實", "grok_chains": "Grok 事件鏈(宣告時間窗)",
    "grok_market_risk": "Grok 大盤風險(SPY 5 日)", "grok_calls": "Grok 轉述喊單(5 日超額)",
    "muse_calls": "Muse 喊單(5 日超額)", "muse_alert": "Muse ALERT elevated/critical(5 日超額)",
    "grok_alert": "Grok ALERT elevated/critical(5 日超額)",
    "muse_alert_block": "Muse ALERT 擋單", "muse_risk_flags_block": "Muse 風險旗標擋單",
    "research_veto_block": "研究否決擋單(Muse/Grok/申報)", "muse_legacy_rule": "舊 Muse 規則(會擋未擋)",
    "grok_options_plan:live": "Grok 期權劇本(紙上實單)", "grok_options_plan:shadow": "Grok 期權劇本(影子)",
}
OBS_COLS = ["day", "source", "hit", "signed", "conf", "pnl"]


def ensure_schema(con) -> None:
    con.executescript(SCHEMA)


def _has(con, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone() is not None


def _num(x) -> float | None:
    return None if x is None or pd.isna(x) else float(x)


def _et(ts: str) -> datetime:
    t = datetime.fromisoformat(ts)
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(ET)


def _sym(t: str) -> str:
    return str(t).strip().upper().replace(".", "-")


def _obs(day, source, hit=None, signed=None, conf=None, pnl=None) -> dict:
    return {"day": str(day), "source": source, "hit": hit, "signed": signed, "conf": conf, "pnl": pnl}


def session_for(received_at: str, sessions: pd.DatetimeIndex) -> pd.Timestamp | None:
    """First session whose regular open came after receipt: the same day when
    received before 09:30 ET on a session day, else the next session."""
    et = _et(received_at)
    day = pd.Timestamp(et.date())
    ok = sessions[sessions >= day] if et.time() < OPEN_ET else sessions[sessions > day]
    return ok[0] if len(ok) else None


def in_play_obs(con, bars: Bars) -> list[dict]:
    """GROK_DAILY.in_play items scored open-to-close on their session D.
    One observation per (ticker, direction, D); the first receipt wins."""
    if not _has(con, "bridge_messages"):
        return []
    out, seen = [], set()
    for r in con.execute("SELECT received_at, parsed_json FROM bridge_messages WHERE type='GROK_DAILY' "
                         "ORDER BY received_at, msg_id").fetchall():
        try:
            items = (json.loads(r["parsed_json"]) or {}).get("in_play") or []
        except (ValueError, AttributeError):
            continue
        d = session_for(r["received_at"], bars.dates)
        if d is None or not isinstance(items, list):
            continue
        for it in items:
            try:
                sym, direction = _sym(it["ticker"]), it["direction"]
            except (KeyError, TypeError):
                continue
            if direction not in ("up", "down") or sym not in bars.close.columns or (sym, direction, d) in seen:
                continue
            o, c = _num(bars.open.at[d, sym]), _num(bars.close.at[d, sym])
            if not o or c is None:
                continue
            seen.add((sym, direction, d))
            signed = (c / o - 1) * (1 if direction == "up" else -1)
            hit = int(signed > 0)
            conf = it.get("confidence")
            conf = float(conf) if isinstance(conf, (int, float)) and 0 <= conf <= 1 else None
            day = d.date()
            out.append(_obs(day, "grok_in_play", hit, signed, conf))
            split = "grok_in_play:confirmed" if it.get("confirmed") is True else "grok_in_play:unconfirmed"
            out.append(_obs(day, split, hit, signed, conf))
    return out


def chain_obs(con) -> list[dict]:
    if not (_has(con, "chain_scores") and _has(con, "chains")):
        return []
    rows = con.execute("SELECT c.created_at, s.direction, s.excess_ret, s.hit, s.confidence FROM chain_scores s "
                       "JOIN chains c ON c.chain_id=s.chain_id "
                       "WHERE s.declared=1 AND c.falsifiable=1 AND c.source='grok'").fetchall()
    return [_obs(_et(r["created_at"]).date(), "grok_chains", r["hit"],
                 r["excess_ret"] * (1 if r["direction"] == "up" else -1), r["confidence"]) for r in rows]


def market_risk_obs(con) -> list[dict]:
    if not _has(con, "grok_market_risk"):
        return []
    out = []
    for r in con.execute("SELECT base_day, level, ret_5 FROM grok_market_risk WHERE ret_5 IS NOT NULL").fetchall():
        sign = 1 if r["level"] == "low" else -1
        signed = r["ret_5"] * sign
        out.append(_obs(r["base_day"], "grok_market_risk", int(signed > 0) if r["ret_5"] != 0 else 0, signed))
    return out


def calls_obs(con) -> list[dict]:
    if not (_has(con, "call_scores") and _has(con, "bridge_messages")):
        return []
    rows = con.execute("SELECT s.base_date, s.excess_ret, s.hit, a.direction, b.type FROM call_scores s "
                       "JOIN author_calls a USING (call_id) JOIN bridge_messages b ON b.msg_id=a.source_msg "
                       "WHERE s.horizon=5").fetchall()
    side = {"long": 1, "short": -1}
    return [_obs(r["base_date"], "muse_calls" if r["type"] == "CALLS" else "grok_calls", r["hit"],
                 r["excess_ret"] * side.get(r["direction"], 0)) for r in rows if r["direction"] in side]


def alert_obs(con, bars: Bars) -> list[dict]:
    """Elevated/critical ALERTs: 5-session excess vs SPY from the first
    complete close after receipt. One per (sender, ticker, ET receipt day);
    Grok's own-channel ALERTs score as grok_alert, everything else as muse_alert."""
    if not _has(con, "bridge_messages") or MARKET not in bars.close.columns:
        return []
    from stockmoney.desk.bridge.inbox import SOURCE_MUSE, has_source_column, source_of
    from stockmoney.desk.learn.calls import base_session
    sessions, spy = bars.dates, bars.close[MARKET]
    out, seen = [], set()
    src_col = "source" if has_source_column(con) else "NULL AS source"
    for r in con.execute(f"SELECT received_at, parsed_json, {src_col} FROM bridge_messages WHERE type='ALERT' "
                         "ORDER BY received_at, msg_id").fetchall():
        label = "muse_alert" if source_of(r["source"], "ALERT") == SOURCE_MUSE else "grok_alert"
        try:
            m = json.loads(r["parsed_json"]) or {}
            sym, sev = _sym(m["ticker"]), m.get("severity")
        except (ValueError, AttributeError, KeyError, TypeError):
            continue
        key = (label, sym, _et(r["received_at"]).date())
        if sev not in ("elevated", "critical") or key in seen or sym not in bars.close.columns:
            continue
        seen.add(key)
        base = base_session(r["received_at"], sessions)
        if base is None:
            continue
        b = sessions.get_loc(base)
        if b + 5 >= len(sessions):
            continue
        t = sessions[b + 5]
        px0, px1, s0, s1 = (_num(x) for x in (bars.close.at[base, sym], bars.close.at[t, sym], spy.at[base], spy.at[t]))
        if None in (px0, px1, s0, s1):
            continue
        ex = (px1 / px0 - 1) - (s1 / s0 - 1)
        out.append(_obs(base.date(), label, int(ex < 0), -ex))
    return out


def block_source(block_class: str, note: str | None, legacy_only) -> str | None:
    if legacy_only == 1:
        return "muse_legacy_rule"
    if "research veto" in (note or ""):
        return "research_veto_block"
    return {"muse_alert": "muse_alert_block", "muse_risk_flags": "muse_risk_flags_block",
            "muse_other": "muse_other_block"}.get(block_class)  # 'other' = reconcile/risk, not an input source


def block_obs(con) -> list[dict]:
    if not _has(con, "blocked_counterfactual"):
        return []
    cols = {r[1] for r in con.execute("PRAGMA table_info(blocked_counterfactual)")}
    legacy = "legacy_only" if "legacy_only" in cols else "0"
    out = []
    for r in con.execute(f"SELECT plan_date, block_class, note, excess_5, {legacy} AS legacy_only "
                         "FROM blocked_counterfactual WHERE first_block=1 AND excess_5 IS NOT NULL").fetchall():
        src = block_source(r["block_class"], r["note"], r["legacy_only"])
        if src:
            out.append(_obs(r["plan_date"], src, int(r["excess_5"] < 0), -r["excess_5"]))
    return out


def options_obs(con) -> list[dict]:
    if not _has(con, "options_positions"):
        return []
    from stockmoney.desk.options.config import GROK
    out = []
    marks = ",".join("?" * len(UNKNOWN_PNL))
    for r in con.execute("SELECT day, mode, source, realized_usd, fees_usd FROM options_positions "
                         f"WHERE state='EXITED' AND COALESCE(exit_reason,'') NOT IN ({marks})", UNKNOWN_PNL).fetchall():
        pnl = (r["realized_usd"] or 0.0) - (r["fees_usd"] or 0.0)
        src = f"grok_options_plan:{r['mode']}" if r["source"] == GROK else f"playbook:{r['source']}:{r['mode']}"
        out.append(_obs(r["day"], src, int(pnl > 0), None, None, pnl))
    return out


def observations(con, bars: Bars) -> pd.DataFrame:
    rows = (in_play_obs(con, bars) + chain_obs(con) + market_risk_obs(con) + calls_obs(con)
            + alert_obs(con, bars) + block_obs(con) + options_obs(con))
    return pd.DataFrame(rows, columns=OBS_COLS)


def metrics(g: pd.DataFrame) -> list[tuple[str, int, float | None]]:
    """(metric, n, value) for one source in one window. ``n`` is the number of
    observations that carry the metric's input."""
    out = [("n", len(g), float(len(g)))]
    h = g["hit"].dropna()
    if g["pnl"].notna().any():
        p = g["pnl"].dropna().astype(float)
        out += [("win_rate", len(h), float(h.mean()) if len(h) else None),
                ("pnl_per_trade", len(p), float(p.mean()) if len(p) else None),
                ("pnl_total", len(p), float(p.sum()))]
        return out
    s = g["signed"].dropna().astype(float)
    out += [("hit_rate", len(h), float(h.mean()) if len(h) else None),
            ("mean_signed", len(s), float(s.mean()) if len(s) else None)]
    b = g.dropna(subset=["hit", "conf"])
    if len(b):
        out.append(("brier", len(b), float(((b["conf"].astype(float) - b["hit"].astype(float)) ** 2).mean())))
    return out


def compute(obs: pd.DataFrame, sessions: pd.DatetimeIndex) -> list[dict]:
    """Rows for source_scores (without day/computed_at). Sources in FIXED get
    an n=0 row in every window even without observations."""
    out = []
    sources = list(FIXED) + sorted(set(obs["source"]) - set(FIXED))
    for wname, k in WINDOWS:
        if k is None or not len(sessions):
            w = obs
        else:
            cutoff = str(sessions[-min(k, len(sessions))].date())
            w = obs[obs["day"] >= cutoff]
        for src in sources:
            g = w[w["source"] == src]
            for metric, n, value in metrics(g):
                if metric != "n" and not n:
                    continue
                out.append({"source": src, "metric": metric, "n": n, "value": value, "window": wname})
    return out


def refresh(con, bars: Bars, through: pd.Timestamp) -> dict:
    """Recompute today's rows (day = ``through``). Idempotent: the day's rows
    are replaced. Earlier days stay, so the weekly review can see the path."""
    ensure_schema(con)
    bars = bars.upto(through)
    sessions = bars.dates
    obs = observations(con, bars)
    rows = compute(obs, sessions)
    day = str(through.date())
    con.execute("DELETE FROM source_scores WHERE day=?", (day,))
    now = store.now()
    con.executemany("INSERT INTO source_scores (day, source, metric, n, value, window, computed_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    [(day, r["source"], r["metric"], r["n"], r["value"], r["window"], now) for r in rows])
    return {"rows": len(rows), "observations": len(obs)}


def trend(primary: str, v20: float | None, n20: int, v60: float | None) -> str:
    if n20 < MIN_N or v20 is None or v60 is None:
        return "樣本不足"
    step = HIT_STEP if primary != "pnl_per_trade" else max(PNL_STEP_USD, 0.1 * abs(v60))
    if v20 - v60 > step:
        return "改善"
    if v60 - v20 > step:
        return "惡化"
    return "持平"


def _cell(m: dict, primary: str) -> str:
    n = int(m.get("n", (0, 0))[1] or 0)
    if not n:
        return "0 筆"
    if primary == "pnl_per_trade":
        pt, tot, wr = m["pnl_per_trade"][1], m["pnl_total"][1], m["win_rate"][1]
        return f"{n} 筆,每筆 {pt:+.0f} 美元,勝率 {wr:.0%},合計 {tot:+.0f}"
    hr, ms = m.get("hit_rate", (0, None))[1], m.get("mean_signed", (0, None))[1]
    s = f"{n} 筆,命中 {hr:.0%}" if hr is not None else f"{n} 筆"
    if ms is not None:
        s += f",平均 {ms:+.2%}"
    if "brier" in m:
        s += f",Brier {m['brier'][1]:.3f}({m['brier'][0]} 筆有信心值)"
    return s


def report(con, day: str) -> str:
    ensure_schema(con)
    rows = con.execute("SELECT source, metric, n, value, window FROM source_scores WHERE day=?", (day,)).fetchall()
    head = ["## 來源記分板(只記錄,不影響下單)", "",
            "每個輸入來源一行:方向類看命中率與平均有向報酬(相對 SPY 的超額,in_play 為當日開盤到收盤),"
            "擋單類看被擋標的之後 5 日是否落後 SPY(平均值為擋單避開的超額),期權看每筆扣手續費後損益。"
            f"趨勢比較近 20 與近 60 個交易日;近 20 日少於 {MIN_N} 筆標「樣本不足」。"
            "來源的權限每週依此表檢討,由你決定。", ""]
    if not rows:
        return "\n".join(head + ["今天沒有算出記分板。"])
    by: dict[str, dict[str, dict]] = {}
    for r in rows:
        by.setdefault(r["source"], {}).setdefault(r["window"], {})[r["metric"]] = (r["n"], r["value"])
    sources = [s for s in FIXED if s in by] + sorted(s for s in by if s not in FIXED)
    lines = head + ["| 來源 | 全期 | 近 20 日 | 近 60 日 | 趨勢 |", "|---|---|---|---|---|"]
    for s in sources:
        w = by[s]
        allm, m20, m60 = w.get("all", {}), w.get("20d", {}), w.get("60d", {})
        primary = "pnl_per_trade" if "pnl_per_trade" in allm else "hit_rate"
        n_all = int(allm.get("n", (0, 0))[1] or 0)
        cell_all = _cell(allm, primary) + (",樣本不足" if n_all < MIN_N else "")
        n20 = int(m20.get("n", (0, 0))[1] or 0)
        tr = trend(primary, m20.get(primary, (0, None))[1], n20, m60.get(primary, (0, None))[1])
        label = LABEL.get(s) or s.replace("playbook:", "規則劇本 ").replace(":live", "(紙上實單)").replace(
            ":shadow", "(影子)")
        lines.append(f"| {label} | {cell_all} | {_cell(m20, primary)} | {_cell(m60, primary)} | {tr} |")
    lines += ["", "Grok in_play 只用日線計算開盤到收盤報酬;desk 沒有保存分鐘 K,前 60 分鐘報酬未計算。"
              "Grok 期權劇本只計已觸發並出場的項目,未觸發的項目不計分。"]
    return "\n".join(lines)
