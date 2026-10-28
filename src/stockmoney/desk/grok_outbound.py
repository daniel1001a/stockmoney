"""Desk-to-Grok mail (2026-10-27): a morning DESK_BRIEF and evening RESEARCH_REQUESTs.

Both go only on the Grok channel (GROK_ADDRESS + GROK_SHARED_TOKEN) and only
when it is configured; each is sent at most once per ET day, the state lives
in bridge_ops (``brief:<day>``, ``grokrq:<day>``) and is written only after a
successful send, so a failed send is retried on the next bridge run.

- DESK_BRIEF, at or after 06:30 ET on NYSE trading days: the previous trading
  day's realized P&L per sleeve, its closed trades, the open positions, today's
  earnings calendar (macro: no table yet, omitted) and up to 5 questions about
  moves over 3% in held or traded names.
- RESEARCH_REQUEST, at or after 17:45 ET on trading days once postclose has
  booked the day (none that day if it has not by 21:00 ET): one per name that
  closed a losing trade today or that is held / traded and moved more than 3%
  today; "why did X move", deadline 21:00 ET. Grok answers with RESEARCH_REPLY
  (request_id = the request's msg_id); muse_ops.check_deliveries reports a late one.

Pure reporting: nothing here reads Grok's answers into a decision, places an
order or changes a parameter. Daily returns come from the ohlcv cache
(``data.load``); tests inject ``returns``.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Callable

from stockmoney.desk import muse_ops
from stockmoney.desk.calendar import is_trading_day

log = logging.getLogger(__name__)

ET = muse_ops.ET
BRIEF_AT = time(6, 30)
REQUEST_AT = time(17, 45)
REPLY_BY = time(21, 0)
MOVE = 0.03
MAX_REQUESTS = 10
MAX_QUESTIONS = 5

Returns = Callable[[date], dict]  # day -> {symbol: close-to-close return}


def _has(con, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone() is not None


def _cut(s, n: int) -> str:
    return str(s or "")[:n]


def _et_day(ts: str | None) -> date | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).astimezone(ET).date()
    except (ValueError, TypeError):
        return None


def previous_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def ohlcv_returns(day: date) -> dict:
    """Close-to-close return per symbol for ``day`` from the ohlcv cache; {} when
    the cache has no bar for that day yet."""
    from stockmoney.desk import data
    df = data.load()
    df = df[["date", "symbol", "close"]].copy()
    df["date"] = df["date"].astype(str).str[:10]
    wide = df.pivot_table(index="date", columns="symbol", values="close").sort_index()
    key = day.isoformat()
    if key not in wide.index:
        return {}
    i = list(wide.index).index(key)
    if i == 0:
        return {}
    ret = wide.iloc[i] / wide.iloc[i - 1] - 1
    return {s: float(v) for s, v in ret.items() if v == v}  # drop NaN


# ---------------------------------------------------------------- facts

def _stock_fills(con, day: date) -> list:
    if not _has(con, "fills"):
        return []
    lo = (day - timedelta(days=1)).isoformat()
    hi = (day + timedelta(days=2)).isoformat()
    rows = con.execute("SELECT symbol, strategy, side, qty, price, filled_at, realized_pnl FROM fills "
                       "WHERE filled_at>=? AND filled_at<? ORDER BY filled_at, id", (lo, hi)).fetchall()
    return [r for r in rows if _et_day(r[5]) == day]


def _option_exits(con, day: date) -> list:
    if not _has(con, "options_positions"):
        return []
    rows = con.execute("SELECT underlying, setup_id, qty_initial, entry_price, exit_time, exit_reason, "
                       "realized_usd, fees_usd FROM options_positions WHERE mode='live' AND state='EXITED' "
                       "AND day>=? AND day<=? ORDER BY exit_time", ((day - timedelta(days=7)).isoformat(),
                                                                     day.isoformat())).fetchall()
    return [r for r in rows if _et_day(r[4]) == day]


def _playbook(setup_id: str | None) -> str:
    parts = str(setup_id or "").split(":")
    return parts[1] if len(parts) >= 3 else "options"


def realized_by_sleeve(con, day: date) -> dict[str, float]:
    out: dict[str, float] = {}
    for sym, strat, side, qty, price, at, pnl in _stock_fills(con, day):
        if pnl:
            out[strat] = out.get(strat, 0.0) + float(pnl)
    for r in _option_exits(con, day):
        key = f"options:{_playbook(r[1])}"
        out[key] = out.get(key, 0.0) + float(r[6] or 0) - float(r[7] or 0)
    return {k: round(v, 2) for k, v in out.items()}


def closed_trades(con, day: date) -> list[dict]:
    """Stock trades closed on ``day`` (trade_postmortem when it has the row, else
    the day's sells aggregated per symbol) and live option positions exited."""
    out = []
    pm = {}
    if _has(con, "trade_postmortem"):
        for r in con.execute("SELECT symbol, strategy, entry_px, exit_px, qty, pnl, exit_reason FROM trade_postmortem "
                             "WHERE exit_day=?", (day.isoformat(),)).fetchall():
            pm.setdefault(r[0], []).append(r)
    sells: dict[str, list] = {}
    for r in _stock_fills(con, day):
        if r[2] == "sell":
            sells.setdefault(r[0], []).append(r)
    for sym in sorted(set(pm) | set(sells)):
        if sym in pm:
            for r in pm[sym]:
                out.append(dict(symbol=sym, sleeve="stocks", strategy=r[1], entry=r[2], exit=r[3], qty=r[4] or 0.0,
                                pnl=None if r[5] is None else round(r[5], 2), exit_reason=r[6] or ""))
            continue
        rows = sells[sym]
        qty = sum(float(r[3]) for r in rows)
        px = sum(float(r[3]) * float(r[4]) for r in rows) / qty if qty else None
        out.append(dict(symbol=sym, sleeve="stocks", strategy=rows[0][1], entry=None, exit=px, qty=qty,
                        pnl=round(sum(float(r[6] or 0) for r in rows), 2), exit_reason=""))
    for r in _option_exits(con, day):
        out.append(dict(symbol=r[0], sleeve="options", strategy=_playbook(r[1]), entry=r[3], exit=None,
                        qty=r[2] or 0.0, pnl=round(float(r[6] or 0) - float(r[7] or 0), 2), exit_reason=r[5] or ""))
    return out


def open_positions(con) -> list[dict]:
    out = []
    if _has(con, "sleeve_positions"):
        for sym, strat, qty, avg in con.execute("SELECT symbol, strategy, qty, avg_price FROM sleeve_positions "
                                                "WHERE qty<>0 ORDER BY symbol").fetchall():
            out.append(dict(symbol=sym, sleeve=strat, qty=qty, avg_price=avg))
    if _has(con, "options_positions"):
        for occ, setup, qty, px in con.execute(
                "SELECT occ, setup_id, qty_open, entry_price FROM options_positions WHERE mode='live' "
                "AND state NOT IN ('EXITED','ABANDONED') AND qty_open>0 ORDER BY occ").fetchall():
            out.append(dict(symbol=occ or "?", sleeve=f"options:{_playbook(setup)}", qty=qty, avg_price=px))
    return out


def earnings_today(con, day: date) -> list[dict]:
    if not _has(con, "earnings"):
        return []
    return [dict(what=f"{s} earnings", when=t or "") for s, t in con.execute(
        "SELECT symbol, time_text FROM earnings WHERE date=? AND removed_at IS NULL ORDER BY symbol",
        (day.isoformat(),)).fetchall()]


def _names(con, day: date) -> set[str]:
    """Held now, or traded (any fill / live option exit) on ``day``."""
    names = {p["symbol"] for p in open_positions(con) if p["sleeve"].split(":")[0] != "options"}
    names |= {r[0] for r in _stock_fills(con, day)}
    names |= {r[0] for r in _option_exits(con, day) if r[0]}
    return names


def _moves(con, day: date, returns: Returns) -> list[tuple[str, float]]:
    try:
        ret = returns(day) or {}
    except Exception as e:  # a missing cache never stops the bridge
        log.warning("grok outbound: no returns for %s: %s", day, e)
        return []
    out = [(s, ret[s]) for s in _names(con, day) if s in ret and abs(ret[s]) > MOVE]
    return sorted(out, key=lambda x: -abs(x[1]))


# ---------------------------------------------------------------- messages

def build_brief(con, now: datetime, returns: Returns):
    from stockmoney.desk.bridge import outbox
    from stockmoney.desk.bridge.schema import CalendarItem, ClosedTrade, DeskBrief, OpenPosition, SleevePnl
    day = now.astimezone(ET).date()
    prev = previous_trading_day(day)
    pnl = [SleevePnl(sleeve=_cut(k, 64), realized=v) for k, v in sorted(realized_by_sleeve(con, prev).items())]
    trades = [ClosedTrade(**{**t, "symbol": _cut(t["symbol"], 32), "strategy": _cut(t["strategy"], 64),
                             "exit_reason": _cut(t["exit_reason"], 120)}) for t in closed_trades(con, prev)]
    held = [OpenPosition(**{**p, "symbol": _cut(p["symbol"], 32), "sleeve": _cut(p["sleeve"], 64)})
            for p in open_positions(con)]
    cal = [CalendarItem(**c) for c in earnings_today(con, day)]
    questions = [f"{s} moved {r:+.1%} on {prev.isoformat()}: what drove it, and is it still in play today?"
                 for s, r in _moves(con, prev, returns)[:MAX_QUESTIONS]]
    return DeskBrief(msg_id=outbox.new_msg_id("brief"), sent_at=now, day=day, prev_day=prev,
                     realized_pnl=pnl[:20], closed_trades=trades[:50], open_positions=held[:100],
                     calendar=cal[:30], questions=questions)


def build_requests(con, now: datetime, returns: Returns) -> list:
    from pydantic import ValidationError

    from stockmoney.desk.bridge import outbox
    from stockmoney.desk.bridge.schema import ResearchRequest
    day = now.astimezone(ET).date()
    deadline = datetime.combine(day, REPLY_BY, ET)
    asks: dict[str, list[str]] = {}
    for t in closed_trades(con, day):
        if t["pnl"] is not None and t["pnl"] < 0:
            asks.setdefault(t["symbol"], []).append(
                f"Our {t['sleeve']} {t['strategy']} trade in {t['symbol']} closed at a loss of {t['pnl']:.0f} USD "
                f"today ({_cut(t['exit_reason'], 80) or 'no reason recorded'}). Why did {t['symbol']} move against it?")
    for s, r in _moves(con, day, returns):
        asks.setdefault(s, []).append(f"Why did {s} move {r:+.1%} today? Name the catalyst and cite sources.")
    out = []
    for sym, qs in list(asks.items())[:MAX_REQUESTS]:
        try:
            out.append(ResearchRequest(msg_id=outbox.new_msg_id("rq-grok"), sent_at=now, ticker=sym,
                                       deadline=deadline, questions=[q[:280] for q in qs[:MAX_QUESTIONS]]))
        except ValidationError:  # an OCC symbol or a name the Ticker pattern does not take
            log.info("grok outbound: no research request for %r", sym)
    return out


def _done(con, key: str) -> bool:
    return con.execute("SELECT 1 FROM bridge_ops WHERE key=?", (key,)).fetchone() is not None


def _postclose_done(con, day: date) -> bool:
    """Postclose books the day's sells; the bridge runs apart from it and does
    not wait on the desk lock, so the requests wait for its marker; by
    REPLY_BY without it the day is closed with nothing sent."""
    from stockmoney.desk.run import PLAN_THROUGH_KEY
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone():
        return False
    r = con.execute("SELECT value FROM settings WHERE key=?", (PLAN_THROUGH_KEY,)).fetchone()
    return r is not None and r[0] >= str(day)


def run(con, box, now: datetime, config=None, returns: Returns | None = None) -> list[str]:
    """Send whatever is due. Returns one line per mail sent (for the job log)."""
    from stockmoney.desk.bridge import outbox
    from stockmoney.desk.bridge.mailbox import BridgeConfig, ConfigError
    if config is None:
        try:
            config = BridgeConfig.from_env()
        except ConfigError:
            return []
    if not config.grok_enabled:
        return []
    returns = returns or ohlcv_returns
    muse_ops.ensure_schema(con)
    local = now.astimezone(ET)
    day = local.date()
    if not is_trading_day(day):
        return []
    lines = []
    if local.time() >= BRIEF_AT and not _done(con, f"brief:{day}"):
        msg = build_brief(con, now, returns)
        outbox.send(box, msg, config, channel="grok")
        muse_ops.log_outbound(con, msg, "grok")
        muse_ops._once(con, f"brief:{day}")
        lines.append(f"DESK_BRIEF 已寄給 Grok({msg.msg_id})")
    booked = _postclose_done(con, day)
    if local.time() >= REQUEST_AT and not _done(con, f"grokrq:{day}") and (booked or local.time() >= REPLY_BY):
        # past the reply deadline without postclose: close the day unsent
        msgs = build_requests(con, now, returns) if booked else []
        for m in msgs:
            outbox.send(box, m, config, channel="grok")
            muse_ops.log_outbound(con, m, "grok")
        muse_ops._once(con, f"grokrq:{day}")
        if msgs:
            lines.append(f"已寄 {len(msgs)} 則盤後原因查詢給 Grok:{', '.join(m.ticker for m in msgs)}")
    return lines
