"""Post-mortem cards for closed LIVE (paper) option positions of the intraday
options sleeve, with symptom codes decided by fixed rules. Same table as the
stock cards (trade_postmortem); trade_id = 'opt:' + position_id, strategy =
'options:' + source, symbol = the underlying. Read-only on the options tables.

Shadow positions are never carded: they are the comparison (O2), not trades.

Symptom rules (all from stored rows: options_positions, options_fills,
bridge_messages, the earnings store):
  O2 停損後影子賺錢  exit by premium_stop or invalidation while a shadow twin of the
                    same setup (any dte / exit variant) closed with pnl > 0
  O6 財報在持有期間  an earnings date of the underlying in [entry day, exit day]; only
                    when the span was fully scanned and the underlying is in the
                    earnings watch list (otherwise unknown, no code)
  O7 獲利回吐        best bid reached 1.5x the entry premium but the position closed
                    at a loss or exited below the entry premium
  O8 執行異常        a fill outside its quote at fill (buy above ask, sell below bid)
                    or a quoted spread above 20% of mid at a fill
  O9 消息面          a Muse flag recorded at entry (flags_json.muse_flag), or a
                    Muse/Grok ALERT on the underlying in the 24 hours before entry
  OQ 快速出場        exited within 10 minutes of the entry
  O0 正常虧損        a loss with none of the above
A card is provisional while a shadow twin of its setup is still open (O2 not
decidable yet), final after; final cards are never rewritten.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from stockmoney.desk import store
from stockmoney.desk.learn import earnings as _earn
from stockmoney.desk.learn import postmortem
from stockmoney.desk.learn.trades import _table_exists, et_day
from stockmoney.desk.universe import STOCKS

PREFIX = postmortem.OPTION_PREFIX
STOP_REASONS = ("premium_stop", "invalidation")
GIVEBACK_MFE = 1.5        # best bid / entry premium that counts as a real move
WIDE_SPREAD = 0.20        # (ask - bid) / mid at a fill
QUICK_EXIT_MIN = 10.0
ALERT_LOOKBACK_H = 24
OPEN_STATES = ("PENDING", "ENTERED")
EPS = 1e-9

LABEL = {"O2": "停損後影子賺錢", "O6": "財報在持有期間", "O7": "獲利回吐", "O8": "執行異常",
         "O9": "消息面", "OQ": "快速出場", "O0": "正常虧損"}


def _when(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo is not None else d.replace(tzinfo=timezone.utc)


def _pnl(p: dict) -> float:
    return (p["realized_usd"] or 0.0) - (p["fees_usd"] or 0.0)


def closed_positions(con) -> list[dict]:
    """Closed live positions (state EXITED with an entry price), in exit order.
    Shadow, open and abandoned (never filled) positions are not trades."""
    if not _table_exists(con, "options_positions"):
        return []
    return [dict(r) for r in con.execute(
        "SELECT * FROM options_positions WHERE mode='live' AND state='EXITED' AND entry_price IS NOT NULL "
        "ORDER BY exit_time, position_id").fetchall()]


def _fills(con, pid: str) -> list[dict]:
    if not _table_exists(con, "options_fills"):
        return []
    return [dict(r) for r in con.execute("SELECT * FROM options_fills WHERE position_id=? ORDER BY at, id", (pid,))]


def _fill_checks(fills: list[dict]) -> tuple[list[dict], int]:
    """(problems, fills without a usable quote) for O8."""
    bad, unquoted = [], 0
    for f in fills:
        bid, ask, px = f["bid"], f["ask"], f["price"]
        if bid is None or ask is None or px is None or ask <= 0 or ask < bid:
            unquoted += 1
            continue
        mid = (bid + ask) / 2
        spread = (ask - bid) / mid if mid > 0 else None
        why = []
        if f["side"] == "buy" and px > ask + EPS:
            why.append("buy_above_ask")
        if f["side"] == "sell" and px < bid - EPS:
            why.append("sell_below_bid")
        if spread is not None and spread > WIDE_SPREAD:
            why.append("wide_spread")
        if why:
            bad.append({"at": f["at"], "side": f["side"], "price": px, "bid": bid, "ask": ask,
                        "spread_pct": round(spread, 4) if spread is not None else None, "why": why})
    return bad, unquoted


def _twins(con, p: dict) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT position_id, state, exit_reason, realized_usd, fees_usd FROM options_positions "
        "WHERE setup_id=? AND mode='shadow' ORDER BY position_id", (p["setup_id"],)).fetchall()]


def _alerts_before(con, sym: str, entry: datetime) -> list[str]:
    if not _table_exists(con, "bridge_messages"):
        return []
    lo = entry - timedelta(hours=ALERT_LOOKBACK_H)
    out = []
    for r in con.execute("SELECT received_at, parsed_json FROM bridge_messages WHERE type='ALERT'").fetchall():
        at = _when(r["received_at"])
        if at is None or not lo <= at < entry:  # only what had arrived before the entry
            continue
        try:
            m = json.loads(r["parsed_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(m, dict):
            continue
        if str(m.get("ticker", "")).replace(".", "-").upper() == sym.replace(".", "-").upper():
            out.append(f"{m.get('severity', '?')}: {str(m.get('headline', ''))[:80]}")
    return out


def build_option_card(con, pos: dict) -> dict:
    """Symptoms and facts of one closed live position (no write)."""
    pid, sym, e = pos["position_id"], pos["underlying"], pos["entry_price"]
    entry, exit_ = _when(pos["entry_time"]), _when(pos["exit_time"])
    fills = _fills(con, pid)
    sells = [f for f in fills if f["side"] == "sell" and f["qty"]]
    sold = sum(f["qty"] for f in sells)
    exit_px = sum(f["qty"] * f["price"] for f in sells) / sold if sold else None
    pnl = _pnl(pos)
    held_min = (exit_ - entry).total_seconds() / 60 if entry and exit_ else None
    mfe = pos["max_bid"] / e if pos["max_bid"] is not None and e else None

    twins = _twins(con, pos)
    twins_open = [t["position_id"] for t in twins if t["state"] in OPEN_STATES]
    twins_won = [t["position_id"] for t in twins if t["state"] == "EXITED" and _pnl(t) > 0]

    try:
        flags = json.loads(pos["flags_json"] or "{}")
    except ValueError:
        flags = {}
    muse_flag = str(flags.get("muse_flag") or "") if isinstance(flags, dict) else ""
    alerts = _alerts_before(con, sym, entry) if entry else []

    entry_day, exit_day = pos["day"], et_day(pos["exit_time"]) or pos["day"]
    span = (datetime.fromisoformat(entry_day).date(), datetime.fromisoformat(exit_day).date())
    earn_known = _earn.norm(sym) in {_earn.norm(s) for s in STOCKS} and _earn.covered(con, *span)
    earn_dates = _earn.in_span(con, sym, *span) if earn_known else []

    bad_fills, unquoted = _fill_checks(fills)

    s: list[str] = []
    if pos["exit_reason"] in STOP_REASONS and twins_won:
        s.append("O2")
    if earn_dates:
        s.append("O6")
    if mfe is not None and mfe >= GIVEBACK_MFE and (pnl <= 0 or (exit_px is not None and exit_px < e)):
        s.append("O7")
    if bad_fills:
        s.append("O8")
    if muse_flag or alerts:
        s.append("O9")
    if held_min is not None and held_min <= QUICK_EXIT_MIN:
        s.append("OQ")
    if pnl <= 0 and not s:
        s.append("O0")

    facts = {
        "occ": pos["occ"], "right": pos["right"], "strike": pos["strike"], "expiry": pos["expiry"],
        "dte_label": pos["dte_label"], "exit_variant": pos["exit_variant"], "direction": pos["direction"],
        "uclass": pos["uclass"], "setup_id": pos["setup_id"], "hold_minutes": held_min,
        "mfe_premium_x": mfe, "max_bid": pos["max_bid"], "min_bid": pos["min_bid"],
        "fees_usd": pos["fees_usd"], "spread_cost_usd": pos["spread_cost_usd"],
        "shadow_twins": [{"position_id": t["position_id"], "state": t["state"], "exit_reason": t["exit_reason"],
                          "pnl": _pnl(t)} for t in twins],
        "earnings_in_hold": earn_dates if earn_known else None,
        "muse_flag_at_entry": muse_flag, "alerts_before_entry": alerts,
        "fill_problems": bad_fills, "fills_without_quote": unquoted,
    }
    return {"symptoms": s, "facts": facts, "exit_px": exit_px, "pnl": pnl,
            "status": "provisional" if twins_open else "final", "entry_day": entry_day, "exit_day": exit_day}


def update_option_cards(con) -> dict:
    """Insert missing option cards and refresh provisional ones. Final cards
    and lessons the owner wrote are never overwritten."""
    new = upd = 0
    positions = closed_positions(con)
    gm = postmortem.gm_inputs(con) if positions else None
    for pos in positions:
        tid = PREFIX + pos["position_id"]
        old = con.execute("SELECT status FROM trade_postmortem WHERE trade_id=?", (tid,)).fetchone()
        if old is not None and old["status"] == "final":
            continue
        card = build_option_card(con, pos)
        e, x = pos["entry_price"], card["exit_px"]
        now = store.now()
        con.execute(
            "INSERT INTO trade_postmortem (trade_id, symbol, strategy, entry_day, exit_day, entry_px, exit_px, qty, pnl, "
            "ret, r_multiple, exit_kind, exit_reason, symptoms, facts_json, status, created_at, updated_at, "
            "grok_muse_independent_same_symbol) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(trade_id) DO UPDATE SET "
            "exit_px=excluded.exit_px, pnl=excluded.pnl, ret=excluded.ret, symptoms=excluded.symptoms, "
            "facts_json=excluded.facts_json, status=excluded.status, updated_at=excluded.updated_at, "
            "grok_muse_independent_same_symbol=excluded.grok_muse_independent_same_symbol",
            (tid, pos["underlying"], "options:" + (pos["source"] or ""), card["entry_day"], card["exit_day"], e, x,
             pos["qty_initial"], card["pnl"], x / e - 1 if x is not None and e else None, None,
             "stop" if pos["exit_reason"] in STOP_REASONS else "exit", pos["exit_reason"] or "",
             ",".join(card["symptoms"]), json.dumps(card["facts"], ensure_ascii=False, default=str), card["status"],
             now, now, postmortem.grok_muse_independent(gm, pos["underlying"], pos["entry_time"])))
        if old is None:
            new += 1
        else:
            upd += 1
    return {"new": new, "updated": upd}
