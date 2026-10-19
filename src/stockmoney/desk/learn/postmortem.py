"""Post-mortem card for every closed trade, with symptom codes decided by
fixed rules (never by feel). One bad trade never changes a rule; the weekly
aggregate (symptom_report) only proposes hypotheses that must pass the normal
backtest -> shadow -> owner-approval path (docs/desk/EDGE_PLAN.md 2.2).

Symptom rules (all computed from bars known at the time, or after exit for S2):
  S1 跳空穿停損  stop exit and the exit session opened >= 1 ATR below the prior close
  S2 被洗出場    stop exit and a close back at/above the entry price within 10 sessions
  S3 追高        signal-day close more than 2.5 ATR above its 20-day average
  S4 大盤翻轉    regime fell from risk_on to a worse state, or neutral to risk_off, while held
  S5 板塊退潮    sector was a leader at the signal day and not at exit
  S6 事件跳空    a loss while an earnings date fell inside the hold (learn/earnings.py, only when
                 the span was fully scanned), or a session that opened >= 1.5 ATR below the prior
                 close on >= 2x volume (price proxy for any news gap)
  S7 獲利回吐    best excursion >= 2R but closed below 0.5R
  S8 執行異常    entry or exit fill more than 0.5% away from that session's open
  S9 消息面     a Muse/Grok ALERT for the ticker arrived while held
  S0 正常虧損    a loss with none of the above (expected for a trend system)
Winners are carded too (symptoms may be empty) so wins and losses are compared.
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import pandas as pd

from stockmoney.desk import store, veto
from stockmoney.desk.data import Bars
from stockmoney.desk.indicators import Ind, market_state, sector_ranking
from stockmoney.desk.learn.trades import ClosedTrade, closed_trades
from stockmoney.desk.universe import sector_of

WHIPSAW_SESSIONS = 10
FINAL_AFTER = WHIPSAW_SESSIONS  # a card is provisional until S2 can be decided

LABEL = {
    "S1": "跳空穿停損", "S2": "被洗出場", "S3": "追高", "S4": "大盤翻轉", "S5": "板塊退潮",
    "S6": "事件跳空", "S7": "獲利回吐", "S8": "執行異常", "S9": "消息面", "S0": "正常虧損",
}


def _num(x) -> float | None:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _at(frame: pd.DataFrame, t: pd.Timestamp, sym: str) -> float | None:
    if sym not in frame.columns or t not in frame.index:
        return None
    return _num(frame.at[t, sym])


def _alerts(con, sym: str, entry_at: str, exit_at: str) -> list[str]:
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='bridge_messages'").fetchone() is None:
        return []
    try:
        end = (datetime.fromisoformat(exit_at.replace("Z", "+00:00")) + timedelta(days=1)).isoformat()
    except ValueError:
        end = exit_at
    out = []
    for r in con.execute("SELECT parsed_json FROM bridge_messages WHERE type='ALERT' AND received_at>=? AND received_at<=?",
                         (entry_at, end)).fetchall():
        try:
            m = json.loads(r["parsed_json"])
        except ValueError:
            continue
        if str(m.get("ticker", "")).replace(".", "-") == sym.replace(".", "-"):
            out.append(f"{m.get('severity', '?')}: {str(m.get('headline', ''))[:80]}")
    return out


def build_card(con, t: ClosedTrade, bars: Bars, ind: Ind) -> dict | None:
    idx = bars.dates
    entry_day, exit_day = pd.Timestamp(t.entry_day), pd.Timestamp(t.exit_day)
    if t.symbol not in bars.close.columns or entry_day not in idx or exit_day not in idx:
        return None
    sig_pos = idx.get_loc(entry_day) - 1
    if sig_pos < 0:
        return None
    sig = idx[sig_pos]
    held = idx[(idx >= entry_day) & (idx <= exit_day)]
    sym = t.symbol

    atr_sig = _at(ind.atr20, sig, sym)
    sma20 = _at(ind.sma20, sig, sym)
    close_sig = _at(ind.close, sig, sym)
    ext_atr = (close_sig - sma20) / atr_sig if None not in (close_sig, sma20, atr_sig) and atr_sig else None

    highs = bars.high.loc[held, sym].dropna()
    lows = bars.low.loc[held, sym].dropna()
    mfe = float(highs.max()) / t.entry_px - 1 if len(highs) else None
    mae = float(lows.min()) / t.entry_px - 1 if len(lows) else None
    risk = t.entry_px - t.initial_stop if t.initial_stop and t.initial_stop < t.entry_px else None
    r_mult = (t.exit_px - t.entry_px) / risk if risk else None
    mfe_r = (float(highs.max()) - t.entry_px) / risk if risk and len(highs) else None

    reg_entry = market_state(ind, sig).regime
    reg_exit = market_state(ind, exit_day).regime
    sec = sector_of(sym)
    def _leading(day):
        r = sector_ranking(ind, day)
        row = r[r["etf"] == sec]
        return bool(row["leading"].iloc[0]) if len(row) else None

    lead_entry = _leading(sig) if sec else None
    lead_exit = _leading(exit_day) if sec else None

    gap_hits = []
    for d in held:
        p = idx.get_loc(d) - 1
        if p < 0:
            continue
        pc, op = _at(bars.close, idx[p], sym), _at(bars.open, d, sym)
        a, vr = _at(ind.atr20, idx[p], sym), _at(ind.vol_ratio, d, sym)
        if None in (pc, op, a) or not a:
            continue
        gap = (op - pc) / a
        if gap <= -1.5 and (vr or 0) >= 2.0:
            gap_hits.append((str(d.date()), round(gap, 2)))
    exit_gap = None
    p = idx.get_loc(exit_day) - 1
    if p >= 0:
        pc, op, a = _at(bars.close, idx[p], sym), _at(bars.open, exit_day, sym), _at(ind.atr20, idx[p], sym)
        if None not in (pc, op, a) and a:
            exit_gap = (op - pc) / a

    after = idx[idx > exit_day][:WHIPSAW_SESSIONS]
    back_above = bool((bars.close.loc[after, sym].dropna() >= t.entry_px).any()) if len(after) else False
    complete = len(after) >= WHIPSAW_SESSIONS

    open_in, open_out = _at(bars.open, entry_day, sym), _at(bars.open, exit_day, sym)
    slip_in = abs(t.entry_px / open_in - 1) if open_in else None
    slip_out = abs(t.exit_px / open_out - 1) if open_out else None

    alerts = _alerts(con, sym, t.entry_at, t.exit_at)
    flags_in_hold = veto.flags_between(con, sym, t.entry_at, t.exit_at)
    vetoes_in_hold = veto.blocks_between(con, sym, t.entry_at, t.exit_at)
    from stockmoney.desk.learn import earnings as _earn
    span = (entry_day.date(), exit_day.date())
    earn_known = _earn.covered(con, *span)
    earn_dates = _earn.in_span(con, sym, *span) if earn_known else []

    s: list[str] = []
    if t.exit_kind == "stop" and exit_gap is not None and exit_gap <= -1.0:
        s.append("S1")
    if t.exit_kind == "stop" and back_above:
        s.append("S2")
    if ext_atr is not None and ext_atr > 2.5:
        s.append("S3")
    order = {"risk_off": 0, "neutral": 1, "risk_on": 2}
    if order[reg_exit] < order[reg_entry] and (reg_entry == "risk_on" or reg_exit == "risk_off"):
        s.append("S4")
    if lead_entry and lead_exit is False:
        s.append("S5")
    if gap_hits or (earn_dates and t.pnl <= 0):
        s.append("S6")
    if mfe_r is not None and r_mult is not None and mfe_r >= 2 and r_mult < 0.5:
        s.append("S7")
    if (slip_in or 0) > 0.005 or (slip_out or 0) > 0.005:
        s.append("S8")
    if alerts:
        s.append("S9")
    if t.pnl <= 0 and not s:
        s.append("S0")

    facts = {
        "regime_entry": reg_entry, "regime_exit": reg_exit, "sector": sec,
        "sector_leading_entry": lead_entry, "sector_leading_exit": lead_exit,
        "ext_atr": ext_atr, "mfe": mfe, "mae": mae, "mfe_r": mfe_r, "exit_gap_atr": exit_gap,
        "gap_events": gap_hits, "earnings_in_hold": earn_dates if earn_known else None, "back_above_entry_10d": back_above if complete else None,
        "slip_entry": slip_in, "slip_exit": slip_out, "hold_sessions": len(held) - 1,
        "alerts": alerts, "research_flags_in_hold": flags_in_hold, "veto_blocks_in_hold": vetoes_in_hold,
        "signal_id": t.signal_id, "initial_stop": t.initial_stop,
    }
    return {"symptoms": s, "facts": facts, "r_multiple": r_mult, "status": "final" if complete else "provisional"}


def update_cards(con, bars: Bars, ind: Ind) -> dict:
    """Insert missing cards and refresh provisional ones. Lessons the owner
    wrote are never overwritten."""
    new = upd = 0
    for t in closed_trades(con):
        old = con.execute("SELECT status FROM trade_postmortem WHERE trade_id=?", (t.trade_id,)).fetchone()
        if old is not None and old["status"] == "final":
            continue
        card = build_card(con, t, bars, ind)
        if card is None:
            continue
        now = store.now()
        con.execute(
            "INSERT INTO trade_postmortem (trade_id, symbol, strategy, entry_day, exit_day, entry_px, exit_px, qty, pnl, "
            "ret, r_multiple, exit_kind, exit_reason, symptoms, facts_json, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(trade_id) DO UPDATE SET "
            "symptoms=excluded.symptoms, facts_json=excluded.facts_json, status=excluded.status, "
            "r_multiple=excluded.r_multiple, updated_at=excluded.updated_at",
            (t.trade_id, t.symbol, t.strategy, t.entry_day, t.exit_day, t.entry_px, t.exit_px, t.qty, t.pnl, t.ret,
             card["r_multiple"], t.exit_kind, t.exit_reason, ",".join(card["symptoms"]),
             json.dumps(card["facts"], ensure_ascii=False, default=str), card["status"], now, now))
        if old is None:
            new += 1
        else:
            upd += 1
    return {"new": new, "updated": upd}


def week_stats(con, today: str, weeks: int = 4) -> list[dict]:
    """One dict per trailing 7-day window ending ``today`` (newest first):
    lo, hi (Timestamps), n, wins, loss (symptom -> summed losing pnl), cnt (symptom -> losing trades)."""
    rows = con.execute("SELECT exit_day, strategy, pnl, symptoms FROM trade_postmortem").fetchall()
    end = pd.Timestamp(today)
    out: list[dict] = []
    for w in range(weeks):
        hi = end - pd.Timedelta(days=7 * w)
        lo = hi - pd.Timedelta(days=7)
        loss: dict[str, float] = defaultdict(float)
        cnt: Counter = Counter()
        n = wins = 0
        for r in rows:
            d = pd.Timestamp(r["exit_day"])
            if not (lo < d <= hi):
                continue
            n += 1
            wins += r["pnl"] > 0
            if r["pnl"] > 0:
                continue
            for code in (r["symptoms"] or "").split(","):
                if code:
                    cnt[code] += 1
                    loss[code] += r["pnl"]
        out.append({"lo": lo, "hi": hi, "n": n, "wins": wins, "loss": dict(loss), "cnt": dict(cnt)})
    return out


def candidate_symptoms(stats: list[dict]) -> list[str]:
    """Non-S0 symptoms in the top two by loss in both of the two newest weeks."""
    if len(stats) < 2:
        return []
    tops = [set(sorted((c for c in w["loss"] if c != "S0"), key=lambda c: w["loss"][c])[:2]) for w in stats[:2]]
    return sorted(tops[0] & tops[1])


def hypothesis_text(code: str) -> str:
    return f"{code} {LABEL[code]} 連續兩週排在虧損前兩名:提出規則假設,走回測 + 影子帳戶驗證,不直接改規則。"


def symptom_report(con, today: str, weeks: int = 4) -> tuple[str, list[str]]:
    """Per-week symptom counts and loss totals; returns (text, hypotheses).
    A hypothesis is proposed only when a symptom (other than S0) is in the top
    two by loss for two consecutive weeks."""
    if con.execute("SELECT 1 FROM trade_postmortem LIMIT 1").fetchone() is None:
        return "尚無平倉交易,沒有驗屍卡。", []
    stats = week_stats(con, today, weeks)
    lines = [f"## 驗屍卡週報(截至 {today})", ""]
    for w in stats:
        loss, cnt = w["loss"], w["cnt"]
        top = ", ".join(f"{c} {LABEL.get(c, c)} x{cnt[c]} (${loss[c]:,.0f})"
                        for c in sorted(loss, key=lambda k: loss[k])[:4]) or "無虧損交易"
        lines.append(f"- {w['lo'].date()} 到 {w['hi'].date()}:{w['n']} 筆,勝 {w['wins']};{top}")
    hyp = [hypothesis_text(c) for c in candidate_symptoms(stats)]
    if hyp:
        lines += ["", "### 候選假設", *[f"- {h}" for h in hyp]]
    return "\n".join(lines), hyp
