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
  S10 催化未兌現 a loss although a chain written before the entry called the symbol up, with the
                 entry inside that impact's horizon, and the symbol did not beat SPY over the hold
                 sessions (both legs entry-session open to exit-session close, so fills and slippage
                 do not count; S11 covers those). It says the hold lagged while an up call was
                 live, not that the call failed: the chain may still pay inside its horizon, and
                 chain_scores judges the call itself
  S11 成本超支   entry filled >= 0.25R above the plan reference price (the entry-session open when
                 the plan has none); R = entry minus initial stop. Exits are not counted: stop
                 exits fill at the stop and gaps through it are S1
  S0 正常虧損    a loss with none of the above (expected for a trend system)
Winners are carded too (symptoms may be empty) so wins and losses are compared.
Every card also records the logged wind state (wind.py) known before the entry
and before the exit session (facts wind_entry / wind_exit; NULL without a log).

Weekly hypotheses: at most MAX_HYPOTHESES, ranked by combined two-week loss, each
written as 我認為 X 因為 Y；驗證 Z；失敗條件 W.

grok_muse_independent_same_symbol (every card, stock and option): 1 when a Grok
item (grok_verified messages with a URL only; grok_legacy never counts) and a Muse flag on the same symbol, both received in the 3 days before the
entry, are independent by veto's rule (both sourced, no shared URL or author);
0 when not; NULL when the inputs are unavailable. Built from bridge_messages and
chains at their receipt times, never from the corroboration table (that table is
rebuilt after the fact and is not point-in-time).

Option cards (trade_id 'opt:...') are built by learn/options_postmortem.py with
their own O codes and are reported in their own section, never mixed into the
stock weekly counts or the stock hypotheses.
"""
from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import pandas as pd

from stockmoney.desk import store, veto, wind
from stockmoney.desk.data import Bars
from stockmoney.desk.indicators import Ind, market_state, sector_ranking
from stockmoney.desk.learn import corroboration
from stockmoney.desk.learn.trades import ClosedTrade, _table_exists, closed_trades
from stockmoney.desk.options.config import FEED_CAVEAT
from stockmoney.desk.universe import MARKET, sector_of

WHIPSAW_SESSIONS = 10
GM_WINDOW_DAYS = 3  # Grok and Muse must both name the symbol within this many days before the entry
OPTION_PREFIX = "opt:"
FINAL_AFTER = WHIPSAW_SESSIONS  # a card is provisional until S2 can be decided
SLIP_R = 0.25  # S11: adverse entry slippage, in R
MAX_HYPOTHESES = 3

LABEL = {
    "S1": "跳空穿停損", "S2": "被洗出場", "S3": "追高", "S4": "大盤翻轉", "S5": "板塊退潮",
    "S6": "事件跳空", "S7": "獲利回吐", "S8": "執行異常", "S9": "消息面", "S10": "催化未兌現",
    "S11": "成本超支", "S0": "正常虧損",
}

# (X: the rule idea, Z: how it is tested, W: what kills it) per stock code; Y is the loss evidence.
_ACCEPT = "兩個子區間每筆 95% 下界都高於現行且最大回撤不更深(EDGE_PLAN 2.4)"
HYPOTHESIS = {
    "S1": ("財報或已知事件前一天減半或不持有隔夜能減少跳空穿停損的虧損",
           "歷史重播比較事件前減碼與現行", "減碼版未達 " + _ACCEPT),
    "S2": ("停損太緊,放寬移動停損或給更長持有能少被洗出場",
           "歷史重播比較較寬停損(如 3.5 ATR)與現行,影子帳戶跟進", "放寬版未達 " + _ACCEPT),
    "S3": ("訊號日離 20 日均線超過 2.5 ATR 時不進場或減碼能少虧",
           "歷史重播加上延伸過濾與現行比較", "過濾版未達 " + _ACCEPT + ",或過濾掉的單整體是賺的"),
    "S4": ("大盤從 risk_on 轉弱時提早出場能少虧",
           "歷史重播加上 regime 轉弱出場與現行比較", "提早出場版未達 " + _ACCEPT),
    "S5": ("持有中板塊掉出領先時出場能少虧",
           "歷史重播加上板塊退潮出場與現行比較", "退潮出場版未達 " + _ACCEPT),
    "S6": ("財報日落在持有期間時進場前避開能少虧",
           "歷史重播排除財報跨期的進場與現行比較", "避開版未達 " + _ACCEPT),
    "S7": ("到 2R 後收緊移動停損能保住獲利",
           "歷史重播加上 2R 後收緊停損與現行比較", "收緊版未達 " + _ACCEPT),
    "S8": ("下單方式(限價、時間)造成的滑價可以壓低",
           "比對成交價與開盤價,紙上帳戶試限價單至少 30 筆", "限價版的未成交損失大於省下的滑價"),
    "S9": ("持有中收到同代號 ALERT 時減碼能少虧",
           "影子帳戶在 ALERT 後減碼與現行比較至少 30 筆", "減碼版每筆 95% 下界未高於現行"),
    "S10": ("連鎖推論喊漲的標的在推論未兌現前不該加分或加碼",
            "chains 計分表看喊漲 impact 的超額報酬命中率,至少 50 筆", "喊漲 impact 對 SPY 的超額報酬命中率 Wilson 下界高於 50%"),
    "S11": ("進場價離計畫價超過 0.25R 時放棄這筆能少虧",
            "影子帳戶套用滑價上限與現行比較至少 30 筆", "放棄的單整體是賺的,或上限版每筆 95% 下界未高於現行"),
}
_GENERIC = ("針對這個症狀調整進場或出場規則能少虧", "回測加影子帳戶至少 30 筆", "調整版每筆 95% 下界未高於現行")


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


def gm_inputs(con) -> tuple[list[dict], list[dict]] | None:
    """(Grok items, Muse flags) for the independence column; None when a
    source table is missing or unreadable (the column is then NULL)."""
    if not (_table_exists(con, "bridge_messages") and _table_exists(con, "chains")):
        return None
    try:
        return corroboration._grok_items(con, verified_only=True), corroboration._muse_flags(con)
    except sqlite3.Error:
        return None


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo is not None else d.replace(tzinfo=timezone.utc)


def grok_muse_independent(inputs: tuple[list[dict], list[dict]] | None, symbol: str, entry_at: str | None) -> int | None:
    """1 when some Grok item and some Muse flag on ``symbol``, both received in
    [entry - GM_WINDOW_DAYS, entry), are independent (veto._muse_grok_independent);
    0 otherwise; None when the inputs or the entry time are unknown."""
    if inputs is None or not entry_at:
        return None
    entry = corroboration._when(entry_at)
    if entry is None:
        return None
    entry = _aware(entry)
    lo = entry - timedelta(days=GM_WINDOW_DAYS)
    tick = corroboration._tick(symbol)

    def _before(items: list[dict]) -> list[dict]:
        return [i for i in items if i["ticker"] == tick and i["at"] is not None and lo <= _aware(i["at"]) < entry]

    grok, muse = _before(inputs[0]), _before(inputs[1])
    return int(bool(grok and muse and veto._muse_grok_independent(muse, grok)))


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


def _wind(con, day: str) -> str | None:
    return wind.state_before(con, day) if _table_exists(con, "wind_daily") else None


def _up_chains(con, sym: str, entry_at: str, entry_day: pd.Timestamp, idx: pd.DatetimeIndex) -> list[str]:
    """Chain ids written before the entry that call ``sym`` up, with the entry
    session no later than horizon_days sessions after the chain's base close."""
    if not _table_exists(con, "chains") or not entry_at:
        return []
    from stockmoney.desk.learn.chains import base_session
    entry = corroboration._when(entry_at)
    if entry is None:
        return []
    entry = _aware(entry)
    pos = idx.get_loc(entry_day)
    out = []
    for r in con.execute("SELECT chain_id, created_at, impacts_json FROM chains").fetchall():
        try:
            created = _aware(datetime.fromisoformat(r["created_at"]))
            impacts = json.loads(r["impacts_json"])
        except (TypeError, ValueError):
            continue
        if created > entry:
            continue
        base = base_session(created.isoformat(), idx)
        if base is None:
            continue
        for imp in impacts:
            if (str(imp.get("ticker", "")).replace(".", "-") == sym.replace(".", "-") and imp.get("direction") == "up"
                    and pos - idx.get_loc(base) <= int(imp.get("horizon_days", 0))):
                out.append(r["chain_id"])
                break
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

    slip_r = None
    ref = t.plan_ref_price or open_in
    if ref and risk:
        slip_r = (t.entry_px - ref) / risk
    catalyst = _up_chains(con, sym, t.entry_at, entry_day, idx)
    excess = None
    spy_in, spy_out = _at(bars.open, entry_day, MARKET), _at(bars.close, exit_day, MARKET)
    sym_out = _at(bars.close, exit_day, sym)
    if spy_in and spy_out and open_in and sym_out:
        excess = (sym_out / open_in - 1) - (spy_out / spy_in - 1)

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
    if t.pnl <= 0 and catalyst and excess is not None and excess <= 0:
        s.append("S10")
    if slip_r is not None and slip_r >= SLIP_R:
        s.append("S11")
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
        "up_chains_before_entry": catalyst, "excess_vs_spy": excess, "slip_entry_r": slip_r,
        "wind_entry": _wind(con, t.entry_day), "wind_exit": _wind(con, t.exit_day),
    }
    return {"symptoms": s, "facts": facts, "r_multiple": r_mult, "status": "final" if complete else "provisional"}


CARD_V = 2  # S10/S11 and the wind facts


def _final_is_current(con, old, t: ClosedTrade) -> bool:
    f = json.loads(old["facts_json"] or "{}")
    if f.get("card_v") != CARD_V:
        return False
    return not (f.get("wind_entry") is None and _wind(con, t.entry_day) is not None)


def update_cards(con, bars: Bars, ind: Ind, *, inputs_ok: bool = True, rebuild_finals: bool = False) -> dict:
    """Insert missing cards and refresh provisional ones. A final card is
    rebuilt until it carries ``card_v`` (stamped only when ``inputs_ok``: this
    run's chain ingest and wind backfill finished), when its entry wind was
    null and the log now has it, and on every run with ``rebuild_finals`` (new
    chains were ingested, S10 may change). Lessons the owner wrote are never
    overwritten."""
    new = upd = 0
    gm = gm_inputs(con)
    for t in closed_trades(con):
        old = con.execute("SELECT status, facts_json FROM trade_postmortem WHERE trade_id=?",
                          (t.trade_id,)).fetchone()
        if old is not None and old["status"] == "final" and not rebuild_finals and _final_is_current(con, old, t):
            continue
        card = build_card(con, t, bars, ind)
        if card is None:
            continue
        if inputs_ok:
            card["facts"]["card_v"] = CARD_V
        now = store.now()
        con.execute(
            "INSERT INTO trade_postmortem (trade_id, symbol, strategy, entry_day, exit_day, entry_px, exit_px, qty, pnl, "
            "ret, r_multiple, exit_kind, exit_reason, symptoms, facts_json, status, created_at, updated_at, "
            "grok_muse_independent_same_symbol) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(trade_id) DO UPDATE SET "
            "symptoms=excluded.symptoms, facts_json=excluded.facts_json, status=excluded.status, "
            "r_multiple=excluded.r_multiple, updated_at=excluded.updated_at, "
            "grok_muse_independent_same_symbol=excluded.grok_muse_independent_same_symbol",
            (t.trade_id, t.symbol, t.strategy, t.entry_day, t.exit_day, t.entry_px, t.exit_px, t.qty, t.pnl, t.ret,
             card["r_multiple"], t.exit_kind, t.exit_reason, ",".join(card["symptoms"]),
             json.dumps(card["facts"], ensure_ascii=False, default=str), card["status"], now, now,
             grok_muse_independent(gm, t.symbol, t.entry_at)))
        if old is None:
            new += 1
        else:
            upd += 1
    return {"new": new, "updated": upd}


def week_stats(con, today: str, weeks: int = 4, book: str = "stock") -> list[dict]:
    """One dict per trailing 7-day window ending ``today`` (newest first):
    lo, hi (Timestamps), n, wins, loss (symptom -> summed losing pnl), cnt (symptom -> losing trades).
    ``book`` 'stock' (default) excludes option cards, 'options' keeps only them."""
    op = "NOT LIKE" if book == "stock" else "LIKE"
    rows = con.execute(f"SELECT exit_day, strategy, pnl, symptoms FROM trade_postmortem WHERE trade_id {op} ?",
                       (OPTION_PREFIX + "%",)).fetchall()
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
    """Symptoms other than S0 / O0 in the top two by loss in both of the two
    newest weeks, largest combined loss first."""
    if len(stats) < 2:
        return []
    tops = [set(sorted((c for c in w["loss"] if c not in ("S0", "O0")), key=lambda c: w["loss"][c])[:2])
            for w in stats[:2]]
    return sorted(tops[0] & tops[1], key=lambda c: (_combined(stats, c), c))


def _combined(stats: list[dict], code: str) -> float:
    return stats[0]["loss"].get(code, 0.0) + stats[1]["loss"].get(code, 0.0)


def _label(code: str) -> str:
    from stockmoney.desk.learn.options_postmortem import LABEL as OPTION_LABEL
    return LABEL.get(code) or OPTION_LABEL.get(code, code)


def top_candidates(con, today: str) -> list[tuple[str, list[dict]]]:
    """The weekly hypothesis candidates: stock and option candidates ranked
    together by two-week loss (largest first), at most MAX_HYPOTHESES, each with
    its own book's week stats (newest first). The report and the recorded
    weekly_hypotheses both use this one list."""
    out = []
    for book in ("stock", "options"):
        stats = week_stats(con, today, 2, book=book)
        out += [(_combined(stats, c), c, stats) for c in candidate_symptoms(stats)]
    return [(c, st) for _, c, st in sorted(out, key=lambda x: (x[0], x[1]))[:MAX_HYPOTHESES]]


def hypothesis_text(code: str) -> str:
    x, z, w = HYPOTHESIS.get(code, _GENERIC)
    return (f"我認為{x},因為 {code} {_label(code)} 連續兩週排在虧損前兩名;驗證:{z};失敗條件:{w}。"
            "只是假設,不直接改規則。")


def _week_lines(stats: list[dict]) -> list[str]:
    lines = []
    for w in stats:
        loss, cnt = w["loss"], w["cnt"]
        top = ", ".join(f"{c} {_label(c)} x{cnt[c]} (${loss[c]:,.0f})"
                        for c in sorted(loss, key=lambda k: loss[k])[:4]) or "無虧損交易"
        lines.append(f"- {w['lo'].date()} 到 {w['hi'].date()}:{w['n']} 筆,勝 {w['wins']};{top}")
    return lines


def symptom_report(con, today: str, weeks: int = 4) -> tuple[str, list[str]]:
    """Per-week symptom counts and loss totals; returns (text, hypotheses).
    A hypothesis is proposed only when a symptom (other than S0) is in the top
    two by loss for two consecutive weeks; at most MAX_HYPOTHESES across both books. Option cards get their own
    section and their own candidates (never mixed with the stock counts)."""
    if con.execute("SELECT 1 FROM trade_postmortem LIMIT 1").fetchone() is None:
        return "尚無平倉交易,沒有驗屍卡。", []
    stats = week_stats(con, today, weeks)
    lines = [f"## 驗屍卡週報(截至 {today})", "", *_week_lines(stats)]
    if con.execute("SELECT 1 FROM trade_postmortem WHERE trade_id LIKE ? LIMIT 1", (OPTION_PREFIX + "%",)).fetchone():
        ostats = week_stats(con, today, weeks, book="options")
        lines += ["", "### 期權驗屍卡(實盤紙上)", f"> {FEED_CAVEAT}", *_week_lines(ostats)]
    hyp = [hypothesis_text(c) for c, _ in top_candidates(con, today)]
    if hyp:
        lines += ["", "### 候選假設", *[f"- {h}" for h in hyp]]
    return "\n".join(lines), hyp
