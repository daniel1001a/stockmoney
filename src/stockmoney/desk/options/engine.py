"""The options sleeve engine: one state machine per setup, driven by an
injectable clock and broker, so the same code runs the live session and the
historical replay.

Setup states:  WAITING -> TRIGGERED -> ENTERED -> EXITED
  with terminal side exits: EXPIRED (window passed without a trigger),
  MISSED (trigger seen too late, e.g. after a restart: never chased),
  SKIPPED (triggered but no tradable contract / underlying filter / already
  invalidated), SUPERSEDED (a later Grok plan replaced this one).
Position states (one setup has several positions):
  shadow: ENTERED -> EXITED
  live:   PENDING (entry order working) -> ENTERED -> EXITED, or ABANDONED
          (entry never filled).

Every trigger opens a SHADOW position per variant (DTE bucket x exit
variant A/B; one delta band per underlying class), filled at the ask by the
shadow model, regardless of live eligibility. A setup that is live-eligible
(config ``live_sources``, the session is live, the broker is paper unless
config says otherwise, the plan arrived before 09:20 ET, no Grok avoid
entry) additionally sends ONE real order for the live variant (1-3 DTE,
never a contract expiring today, exit A), but only if its RVOL is known
(and, for a Grok plan, the playbook is gap_go / orb with day RVOL >= the
live floor), no macro event is within 30 minutes, HALT is absent, Muse has
no block flag on the underlying, the engine's own position cap is free and the
injected risk gate (_iface.RiskGate: line A's OptionsRiskEngine in a live
session) allows it. The risk gate may shrink an order, never grow it. After
a submit the gate RESERVES the premium; every live fill is booked in line
A's OptionsBook (idempotent per client_order_id) and the reservation is
RELEASED once the entry is booked or ends unfilled.

Exits (OPTIONS_PLAN.md section 5, the same for shadow and live, checked in
this order): forced flat at 15:45 ET (on an early close: the close minus
15 minutes, e.g. 12:45 ET; see "Session clock" below); underlying invalidation (a price
level crossed by the quote, a completed 1-minute close beyond a level, or a
close back across VWAP, depending on the playbook); time stop (180 min, or
the plan's shorter max_hold_minutes); then by exit variant: A = premium stop
-35% on the quote mid, +40% on the mid takes half (all of a single contract)
and moves the stop on the rest to cost; B = premium stop -30%, +40% takes
half, the rest trails the underlying by 0.4 x ATR(14) of its 1-minute bars.
Premium exits need a fresh quote; the rest do not. Every live exit passes
the risk gate (check_exit: never more than the broker holds, so never a
sell to open); a limit sell the gate refuses falls back to a market sell.

Session clock: the day's close comes from the broker's market calendar when
it has one (Alpaca) and from desk.calendar.EARLY_CLOSES otherwise (the
earlier of the two wins). Forced flat = min(15:45, close - 15 min); the
risk gate's flat_by is set to the same time; the time stop of a new entry
is capped by it; no live entry with less than 90 minutes left before it.
The owner is paged (CRITICAL) if the book is not flat 10 minutes before the
close (15:50 ET on a normal day), and again by finish().

Leftovers: a live row of an EARLIER day still PENDING / ENTERED (the session
died, the Mac slept) is resolved every tick and sold at market from the
open (09:30), the first tick of the session; shadow leftovers are closed at
their last known bid. On the first tick of a day the broker's option
positions are compared with the store and the options book, and any
position the store does not know is paged CRITICAL (never sold: it may be
the owner's own trade). A partly filled entry stuck in PENDING (its cancel
keeps failing) gets exits too: from the forced flat, when its time stop
elapses, or when it is left over, the filled part is sold.

Option prices: the account has only the "indicative" option feed and no
real-time option 1-minute bars (OPTIONS_PLAN.md sections 10 and 12). Every
option price here is a polled snapshot, so MFE / MAE are the best / worst
BID seen at the poll times (every ``poll_seconds``), not intrabar extremes.

Idempotency and restarts: all state lives in the options_* tables, keyed by
deterministic ids. A tick re-reads that state, so re-running a tick or
restarting the session resumes where it was. An order row is committed as
pending_submit BEFORE broker.submit, with a deterministic client_order_id,
so a crash in between can never lead to a second submission; the next tick
resolves the row with broker.get_order.

Data safety: no entry on a stale underlying bar (``max_bar_age_s``) or a
stale option quote (``max_quote_age_s``); errors are logged per symbol /
position and never stop the loop; exits are retried every tick.
"""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from math import floor
from typing import Callable
from zoneinfo import ZoneInfo

import pandas as pd

from stockmoney.desk import notify
from stockmoney.desk.calendar import close_time, is_trading_day
from stockmoney.desk.options import plan as oplan
from stockmoney.desk.options import playbooks as pb
from stockmoney.desk.options import select as sel
from stockmoney.desk.options import shadow as sh
from stockmoney.desk.options import store as ostore
from stockmoney.desk.options import universe as uni
from stockmoney.desk.options._iface import (
    TIF, AssetClass, OptionContract, OptionsBook, OrderIntent, OrderStatus, OrderType, RiskDecision, RiskGate, Side,
)
from stockmoney.desk.options.config import GROK, RULE_PLAYBOOKS, OptionsConfig

log = logging.getLogger("desk.options")
ET = ZoneInfo("America/New_York")

WAITING, TRIGGERED, ENTERED, EXITED = "WAITING", "TRIGGERED", "ENTERED", "EXITED"
EXPIRED, MISSED, SKIPPED, SUPERSEDED = "EXPIRED", "MISSED", "SKIPPED", "SUPERSEDED"
PENDING, ABANDONED = "PENDING", "ABANDONED"
# "unresolved": an exit whose status could not be read for a while and was
# superseded by a fresh market sell; it is still polled to book late fills
UNRESOLVED = "unresolved"
_TERMINAL_ORDER = {OrderStatus.FILLED.value, OrderStatus.CANCELED.value, OrderStatus.EXPIRED.value,
                   OrderStatus.REJECTED.value, "not_sent", UNRESOLVED}
NOT_SENT_AFTER_S = 30.0  # a pending_submit row the broker still does not know after this was never sent


def _et(now: datetime) -> datetime:
    return now.astimezone(ET)


def _at(day: date, hm: tuple[int, int]) -> datetime:
    return datetime.combine(day, time(*hm), ET)


def _prev_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def broker_close(broker, day: date) -> tuple[int, int] | None:
    """Close of ``day`` from the broker's market calendar (Alpaca's, when the
    broker exposes a trading client); None when the broker has no calendar
    or the day is not in it. Raises when the calendar cannot be read."""
    client = getattr(broker, "client", None)
    if client is None or not hasattr(client, "get_calendar"):
        return None
    from alpaca.trading.requests import GetCalendarRequest
    req = GetCalendarRequest(start=day, end=day)
    retry = getattr(broker, "_retry", None)
    cal = retry(lambda: client.get_calendar(req), "get_calendar") if callable(retry) else client.get_calendar(req)
    for c in cal:
        if c.date == day:  # open / close are naive ET wall-clock datetimes
            return (c.close.hour, c.close.minute)
    return None


def market_close(broker, day: date) -> tuple[int, int]:
    """Close of ``day`` in ET (hour, minute): the earlier of the broker's
    market calendar and desk.calendar.close_time, so an early close known to
    either is honored. A calendar error falls back to the built-in list."""
    hm = close_time(day)
    try:
        b = broker_close(broker, day)
    except Exception as e:
        log.warning("market calendar unavailable, using the built-in early-close list: %s", e)
        return hm
    return min(hm, b) if b else hm


class Engine:
    def __init__(self, con, broker, cfg: OptionsConfig | None = None, *, clock: Callable[[], datetime],
                 risk_gate: RiskGate, live: bool = False,
                 flags_fn: Callable[[datetime], dict[str, str]] | None = None,
                 notifier: Callable[..., object] | None = None):
        self.con = con
        self.broker = broker
        self.cfg = cfg or OptionsConfig()
        self.clock = clock
        self.risk_gate = risk_gate
        paper_ok = bool(getattr(broker, "is_paper", False)) or not self.cfg.paper_only
        self.live = bool(live) and paper_ok and hasattr(broker, "submit")
        self.flags_fn = flags_fn or (lambda now: {})
        self.notifier = notifier or notify.send
        self._bars: dict[str, pd.DataFrame] = {}
        self._profiles: dict[str, pb.VolProfile] = {}
        self._profile_retry: dict[str, datetime] = {}
        self._loss_days: tuple[str, int] | None = None
        self._active = None
        self._errors = 0
        self._plan_seen: str | None = None
        self._closes: dict[date, tuple[int, int]] = {}
        self._cal_fail: dict[date, int] = {}     # failed calendar reads per day (never cached)
        self._cal_retry: datetime | None = None  # no calendar retry before this
        self._flat_day: tuple | None = None    # (day, flat time) last given to the risk gate
        self._swept: str | None = None         # day the start-of-day sweep last ran
        self._cancel_fail: dict[str, int] = {}  # client_order_id -> failed cancels in a row
        self._cancel_next: dict[str, datetime] = {}   # exit client_order_id -> no cancel retry before this
        self._cancel_paged: dict[str, datetime] = {}  # exit client_order_id -> last "cannot cancel" page
        self._status_fail: dict[str, int] = {}  # exit client_order_id -> failed get_order reads in a row
        self._obook: OptionsBook | None = None
        ostore.ensure_schema(con)

    # ================================================================ tick
    def _begin(self, now: datetime) -> None:
        """Per-tick context (also used by recover and finish)."""
        day = _et(now).date()
        self._now, self._day, self._dstr = now, day, str(day)
        self._quotes: dict[str, OptionContract | None] = {}
        self._chains: dict[tuple, list[OptionContract]] = {}
        self._spots: dict[str, float | None] = {}
        self._flags: dict[str, str] | None = None
        self._tick_bars: dict[str, pd.DataFrame] = {}
        self._tick_errors = 0

    def tick(self) -> dict:
        now = self.clock()
        self._begin(now)
        et, day = _et(now), self._day
        self._ensure_day(now)
        halted = self.cfg.halt_file.exists()
        if halted and ostore.notify_once(self.con, f"{self._dstr}:halt"):
            self._notify("期權軌道:HALT 生效", "data/desk/HALT 存在:新進場全擋(影子照記),出場照常。", notify.WARN)
        self._start_of_day(now)
        # resolve working orders and exits before any new entry, so the risk
        # gate sees a book that already includes this tick's fills
        self._manage_pending(now)
        self._manage_open(now)
        if et >= _at(day, self.cfg.open_time):
            for m in ostore.universe(self.con, self._dstr):
                try:
                    self._scan_symbol(m, now, halted)
                except Exception as e:  # one symbol's bad data must not stop the loop
                    self._error("scan", f"{m['symbol']}: {type(e).__name__}: {e}")
        self._roll_setups()
        self._flat_alert(now)
        self._errors = 0 if self._tick_errors == 0 else self._errors
        self.con.commit()
        return self.summary()

    def summary(self) -> dict:
        rows = self.con.execute("SELECT mode, state, COUNT(*) n FROM options_positions WHERE day=? GROUP BY mode, state",
                                (self._dstr,)).fetchall()
        return {f"{r['mode']}_{r['state'].lower()}": r["n"] for r in rows}

    # ================================================================ session clock
    def _close(self) -> datetime:
        """Today's regular-session close (ET), early closes included. A failed
        calendar read is NOT cached: it is retried (at most once a minute) and
        paged after 3 failures; meanwhile the built-in list decides (16:00, or
        a listed early close), so the forced flat is never later than 15:45."""
        day = self._day
        if day in self._closes:
            return _at(day, self._closes[day])
        hm = close_time(day)
        if self._cal_retry is not None and self._now < self._cal_retry:
            return _at(day, hm)
        self._io()
        try:
            b = broker_close(self.broker, day)
        except Exception as e:
            n = self._cal_fail[day] = self._cal_fail.get(day, 0) + 1
            self._cal_retry = self._now + timedelta(seconds=60)
            self._error("calendar", f"market calendar read failed ({n}x): {type(e).__name__}: {e}")
            if n >= 3 and ostore.notify_once(self.con, f"{day}:calendar_unavailable"):
                self._notify("期權軌道:市場日曆讀不到", f"Alpaca 市場日曆連續 {n} 次讀取失敗({e})。暫用內建提早收盤"
                             f"清單:今天收盤視為 {hm[0]:02d}:{hm[1]:02d} ET。若今天其實提早收盤,請手動確認部位。",
                             notify.CRITICAL)
            return _at(day, hm)
        self._closes[day] = min(hm, b) if b else hm
        return _at(day, self._closes[day])

    def _flat(self) -> datetime:
        """Today's forced-flat time: 15:45, or the close minus 15 minutes if earlier."""
        return min(_at(self._day, self.cfg.force_flat),
                   self._close() - timedelta(minutes=self.cfg.flat_before_close_min))

    def session_end(self, now: datetime | None = None) -> datetime:
        """When the session loop stops (the day of ``now``, else of the last
        tick): the close, never after cfg.session_end."""
        if now is not None:
            self._begin(now)
        return min(_at(self._day, self.cfg.session_end), self._close())

    def _live_rows(self, states: tuple[str, ...], today: bool = True) -> list[dict]:
        """Live rows in ``states``: those left over from earlier days first,
        then (``today``) today's."""
        rows = ostore.leftovers(self.con, self._dstr, "live", states)
        return rows + (ostore.positions(self.con, self._dstr, "live", states) if today else [])

    # ================================================================ day setup
    def _ensure_day(self, now: datetime) -> None:
        cfg, d = self.cfg, self._dstr
        flat = (d, self._flat().time())
        if self._flat_day != flat:  # the gate refuses buys from the same (possibly early) flat time
            self._flat_day = flat
            self.risk_gate.set_flat_by(flat[1])
        active = self._active = oplan.active_plan(self.con, self._day)
        if not ostore.universe(self.con, d):
            if _et(now) < _at(self._day, cfg.open_time) - timedelta(minutes=5):
                return  # the scan wants premarket data up to ~09:25
            self._build_universe(now, active)
        if active is not None and active.plan.plan_id != self._plan_seen:
            self._add_plan(active, now)
        self._plan_seen = active.plan.plan_id if active else None

    def _prev_close(self, sym: str) -> float | None:
        """Previous session's last regular-hours minute close (15:59 bar). Not
        the official closing auction print; within a few cents for liquid names."""
        pd_ = _prev_trading_day(self._day)
        try:
            self._io()
            b = self.broker.minute_bars(sym, _at(pd_, (15, 50)), _at(pd_, (16, 0)))
        except Exception as e:
            self._error("prev_close", f"{sym}: {e}")
            return None
        if b is None or b.empty:
            return None
        b = b[pd.DatetimeIndex(b.index).tz_convert(ET) < pd.Timestamp(_at(pd_, (16, 0)))]
        return float(b["close"].iloc[-1]) if not b.empty else None

    def _pm_dollar_vol(self, sym: str, now: datetime) -> float | None:
        """Premarket dollar volume known at ``now``. A broker that offers
        ``premarket_dollar_volume`` (delayed SIP: the live IEX feed has almost
        no premarket prints) is preferred; else the broker's own minute bars."""
        try:
            self._io()
            fn = getattr(self.broker, "premarket_dollar_volume", None)
            if callable(fn):
                return fn(sym, self._day, now)
            b = pb.completed(self.broker.minute_bars(sym, _at(self._day, (4, 0)), now), now)
            pm, _ = pb.split_session(b, self.cfg)
            return float((pm["close"] * pm["volume"]).sum())
        except Exception as e:
            self._error("premarket_volume", f"{sym}: {e}")
            return None

    def _build_universe(self, now: datetime, active) -> None:
        cfg, d = self.cfg, self._dstr
        plan_syms = [p.underlying for p in active.plan.plans] if active else []
        hits = []
        prev: dict[str, float | None] = {}
        for s in cfg.scan_candidates:
            prev[s] = self._prev_close(s)
            try:
                self._io()
                b = pb.completed(self.broker.minute_bars(s, _at(self._day, (4, 0)), now), now)
            except Exception as e:
                self._error("scan", f"{s}: {e}")
                continue
            pm, _ = pb.split_session(b, cfg)
            ok, why, detail = uni.scan_hit(pm, prev[s], cfg)
            if ok:
                hits.append(s)
            else:
                ostore.filtered(self.con, d, now, "universe", s, why, detail=detail)
        members, dropped = uni.build(cfg, plan_syms, hits)
        for s, why in dropped:
            ostore.filtered(self.con, d, now, "universe", s, why)
        rows = [(m.symbol, m.uclass, m.origin, prev.get(m.symbol) or self._prev_close(m.symbol),
                 self._pm_dollar_vol(m.symbol, now)) for m in members]
        ostore.save_universe(self.con, d, rows)
        for m in members:
            self._profile(m.symbol, now)
        avoid = {a.ticker for a in active.plan.avoid} if active else set()
        for m in members:
            for src in RULE_PLAYBOOKS:
                self._new_setup(f"{d}:{src}:{m.symbol}", src, m.symbol, m.uclass, now, avoid=avoid)
        ostore.event(self.con, d, now, "info", "universe", f"{len(members)} underlyings: "
                     + ",".join(m.symbol for m in members))

    def _add_plan(self, active, now: datetime) -> None:
        """Create grok_plan setups for the plan in force (items WITH a
        trigger); WAITING setups of an older plan for the same day are
        superseded (never doubled). An item WITHOUT a trigger (schema v1.1
        directional prior) creates no setup of its own: its underlying joins
        the day's universe (cap permitting) with the rule setups on it, and
        those setups are tagged with grok_stance when they trigger."""
        d, p = self._dstr, active.plan
        self.con.execute("UPDATE options_setups SET state=?, live_note=? WHERE day=? AND source=? AND plan_id<>? "
                         "AND state=?", (SUPERSEDED, f"superseded by {p.plan_id}", d, GROK, p.plan_id, WAITING))
        have = {r["symbol"] for r in ostore.universe(self.con, d)}
        for i, item in enumerate(p.plans):
            sym = item.underlying
            if sym not in have:
                if len(have) >= self.cfg.daily_universe_cap:
                    ostore.filtered(self.con, d, now, "universe", sym, "daily_cap", setup_id=f"{d}:grok:{p.plan_id}:{i}")
                    continue
                origin = "plan_prior" if item.directional else "plan"
                ostore.save_universe(self.con, d, [(sym, self.cfg.underlying_class(sym), origin, self._prev_close(sym),
                                                    self._pm_dollar_vol(sym, now))])
                self._profile(sym, now)
                have.add(sym)
                if item.directional:  # rule setups on a name that joined after the universe was built
                    avoid = {a.ticker for a in p.avoid}
                    for src in RULE_PLAYBOOKS:
                        self._new_setup(f"{d}:{src}:{sym}", src, sym, self.cfg.underlying_class(sym), now, avoid=avoid)
            if item.directional:
                continue
            spec = {"plan_id": p.plan_id, "shadow_only": active.shadow_only, "item": item.model_dump(mode="json"),
                    "received_at": active.received_at.isoformat()}
            self._new_setup(f"{d}:grok:{p.plan_id}:{i}", GROK, sym, self.cfg.underlying_class(sym), now,
                            plan_id=p.plan_id, plan_index=i, spec=spec, plan_shadow_only=active.shadow_only)

    def _new_setup(self, setup_id: str, source: str, sym: str, uclass: str, now: datetime, *, plan_id: str = "",
                   plan_index: int | None = None, spec: dict | None = None, plan_shadow_only: bool = False,
                   avoid: set[str] | None = None) -> None:
        note = self._live_note(source, sym, plan_shadow_only, avoid or set())
        ostore.upsert_setup(self.con, {
            "setup_id": setup_id, "day": self._dstr, "source": source, "plan_id": plan_id, "plan_index": plan_index,
            "underlying": sym, "uclass": uclass, "state": WAITING, "direction": None,
            "spec_json": json.dumps(spec or {}, ensure_ascii=False), "signal_json": None,
            "live_eligible": int(note == ""), "live_note": note, "created_at": ostore.iso(now),
            "updated_at": ostore.iso(now)})

    def _live_note(self, source: str, sym: str, plan_shadow_only: bool, avoid: set[str]) -> str:
        """'' when the setup may send a real (paper) order, else why not."""
        if not self.live:
            return "session is shadow-only"
        if source not in self.cfg.live_sources:
            return "source not live-eligible"
        if source == GROK and plan_shadow_only:
            return "plan received after 09:20 ET"
        if source != GROK and sym in avoid:
            return "on Grok avoid list"
        return ""

    # ================================================================ triggers
    def _scan_symbol(self, m: dict, now: datetime, halted: bool) -> None:
        sym = m["symbol"]
        waiting = [s for s in ostore.setups(self.con, self._dstr) if s["underlying"] == sym and s["state"] == WAITING]
        if not waiting:
            return
        et = _et(now)
        if et >= _at(self._day, self.cfg.entry_window_end) + timedelta(minutes=1):
            for s in waiting:
                ostore.update_setup(self.con, s["setup_id"], state=EXPIRED)
            return
        bars = self._bars_now(sym, now)
        done = pb.completed(bars, now)
        if et >= _at(self._day, self.cfg.open_time) + timedelta(minutes=2):
            last_close = done.index[-1] + pb.ONE_MIN if not done.empty else None
            if last_close is None or (now - last_close.to_pydatetime()).total_seconds() > self.cfg.max_bar_age_s:
                if ostore.notify_once(self.con, f"{self._dstr}:stale_bars:{sym}"):  # once per symbol per day
                    ostore.event(self.con, self._dstr, now, "warn", "stale_bars", f"{sym}: last bar {last_close}")
                    self._notify("期權軌道:資料延遲", f"{sym} 的 1 分鐘 K 過舊({last_close}),暫停該標的的新進場。",
                                 notify.WARN)
                return
        bench = self.cfg.benchmark(sym)
        ctx = {"pm_dollar_vol": m.get("pm_dollar_vol"), "profile": self._profile(sym, now),
               "bench": self._bars_now(bench, now) if bench != sym else None}
        for s in waiting:
            sig = self._evaluate(s, done, now, m.get("prev_close"), ctx)
            if sig is not None:
                self._on_signal(s, sig, done, now, halted)

    def _evaluate(self, s: dict, bars: pd.DataFrame, now: datetime, prev_close, ctx: dict) -> pb.Signal | None:
        if s["source"] == GROK:
            item = oplan.PlanItem.model_validate(json.loads(s["spec_json"])["item"], strict=False)
            return pb.grok_trigger(item, bars, now, self.cfg)
        return pb.RULES[s["source"]](bars, now, prev_close, s["underlying"], self.cfg, ctx)

    def _market_state(self) -> dict:
        b = self._bars.get("SPY")
        if b is None or b.empty:
            return {}
        _, rth = pb.split_session(pb.completed(b, self._now), self.cfg)
        if rth.empty:
            return {}
        u = {r["symbol"]: r for r in ostore.universe(self.con, self._dstr)}
        pc = (u.get("SPY") or {}).get("prev_close")
        last = float(rth["close"].iloc[-1])
        out = {"spy_vs_open": round(last / float(rth["open"].iloc[0]) - 1, 4)}
        if pc:
            out["spy_vs_prev_close"] = round(last / pc - 1, 4)
        return out

    # ---------------------------------------------------------------- regime tags
    def _macro_times(self) -> list[tuple[datetime, str]]:
        """Macro event times today: the active plan's key_events plus the
        config list."""
        out = []
        if self._active is not None:
            for ev in self._active.plan.market_context.key_events:
                out.append((_at(self._day, pb._hm(ev.time)), ev.what))
        for d, hm in self.cfg.macro_events:
            if d == self._dstr:
                out.append((_at(self._day, pb._hm(hm)), "config"))
        return out

    def _consecutive_loss_days(self) -> int:
        """Previous sessions in a row whose closed shadow trades of the live
        variant (live_dte, live_exit) lost money net; a day without such
        trades ends the run. Computed once per day."""
        if self._loss_days is None or self._loss_days[0] != self._dstr:
            rows = self.con.execute(
                "SELECT day, SUM(realized_usd - fees_usd) FROM options_positions WHERE day < ? AND mode='shadow' "
                "AND state='EXITED' AND dte_label=? AND COALESCE(exit_variant, 'A')=? GROUP BY day ORDER BY day DESC",
                (self._dstr, self.cfg.live_dte, self.cfg.live_exit)).fetchall()
            net = {r[0]: r[1] for r in rows}
            n, d = 0, self._day
            while n < 60:
                d = _prev_trading_day(d)
                if net.get(str(d)) is None or net[str(d)] >= 0:
                    break
                n += 1
            self._loss_days = (self._dstr, n)
        return self._loss_days[1]

    def _regime(self, at: datetime, now: datetime) -> dict:
        """Tags stored per setup (not blocking, except the macro window for
        live entries). Only data known at ``at`` is used. ``vix_prev_close``
        has no source in this system yet and is always None (never invented)."""
        cfg = self.cfg
        spy = pb.completed(self._bars_now("SPY", now), at)
        pm, rth = pb.split_session(spy, cfg) if not spy.empty and spy.index[-1].date() == self._day else (spy[:0], spy[:0])
        first15 = None
        if _et(at) >= _at(self._day, (9, 45)) and not rth.empty:
            r15 = rth[rth.index < pd.Timestamp(_at(self._day, (9, 45)))]
            if not r15.empty and float(r15["open"].iloc[0]) > 0:
                first15 = round((float(r15["high"].max()) - float(r15["low"].min())) / float(r15["open"].iloc[0]), 5)
        win = timedelta(minutes=cfg.macro_block_minutes)
        near = [f"{t:%H:%M} {what}" for t, what in self._macro_times() if abs(at - t) <= win]
        pmr = pb.premarket_rvol(pm, self._profile("SPY", now), cfg) if not pm.empty else None
        return {"spy_premarket_rvol": None if pmr is None else round(pmr, 3), "spy_first15_range_pct": first15,
                "vix_prev_close": None, "macro_event_within_30m": bool(near), "macro_events": near,
                "consecutive_loss_days": self._consecutive_loss_days()}

    def _live_block(self, s: dict, sig: pb.Signal, spec: dict, regime: dict) -> str:
        """'' when this trigger may send its live order, else why not
        (signal-level rules; the setup-level ones are in _live_note)."""
        cfg, src, m = self.cfg, s["source"], sig.meta
        if src == "gap_go" and m.get("rvol5") is None:
            return "rvol_unknown"
        if src == "orb15" and m.get("day_rvol") is None:
            return "rvol_unknown"
        if src == GROK:
            item = spec["item"]
            if item["playbook"] not in cfg.grok_live_playbooks:
                return "grok_playbook_shadow_only"
            floor = max(cfg.grok_min_rvol, item.get("min_rvol") or 0.0)
            if m.get("day_rvol") is None:
                return "rvol_unknown"
            if m["day_rvol"] < floor:
                return "rvol_below_live_floor"
        if regime["macro_event_within_30m"]:
            return "macro_event_window"
        return ""

    def _on_signal(self, s: dict, sig: pb.Signal, bars: pd.DataFrame, now: datetime, halted: bool) -> None:
        cfg, d, sid, sym = self.cfg, self._dstr, s["setup_id"], s["underlying"]
        _, rth = pb.split_session(bars, cfg)
        rv = pb.day_rvol(rth, pd.Timestamp(sig.at).tz_convert(ET), self._profiles.get(sym), cfg)
        sig = replace(sig, meta={**sig.meta, "day_rvol": None if rv is None else round(rv, 3)})
        regime = self._regime(sig.at, now)
        tail = [{"t": ts.isoformat(), **{k: float(v) for k, v in r.items() if k in ("open", "high", "low", "close",
                                                                               "volume", "vwap")}}
                for ts, r in bars.tail(5).iterrows()]
        info = {**sig.to_dict(), "bars": tail, "market": self._market_state(), "seen_at": ostore.iso(now)}
        stance, late = self._grok_stance(s, sig.direction)
        ostore.update_setup(self.con, sid, state=TRIGGERED, direction=sig.direction, rvol=sig.meta["day_rvol"],
                            regime_json=json.dumps(regime, ensure_ascii=False),
                            signal_json=json.dumps(info, ensure_ascii=False, default=str),
                            grok_stance=stance, grok_stance_late=late)
        age = (now - sig.at).total_seconds()
        if age > cfg.max_signal_age_s:
            ostore.update_setup(self.con, sid, state=MISSED)
            ostore.filtered(self.con, d, now, "entry", sym, "stale_trigger", sid, detail={"age_s": round(age)})
            return
        et = _et(now)
        if not (_at(self._day, cfg.entry_window_start) <= et <= _at(self._day, cfg.entry_window_end) + timedelta(minutes=1)):
            ostore.update_setup(self.con, sid, state=EXPIRED)
            ostore.filtered(self.con, d, now, "entry", sym, "outside_window", sid)
            return
        self._io()
        q = self.broker.stock_quote(sym)
        if not q or q[0] <= 0 or q[1] < q[0]:
            return self._skip(s, now, "no_underlying_quote")
        spot = (q[0] + q[1]) / 2
        if (q[1] - q[0]) / spot > cfg.underlying_max_spread_pct:
            return self._skip(s, now, "underlying_spread_wide", {"bid": q[0], "ask": q[1]})
        inv = sig.invalidation
        if inv is not None and ((sig.direction == "long" and spot <= inv) or (sig.direction == "short" and spot >= inv)):
            return self._skip(s, now, "already_invalidated", {"spot": spot, "invalidation": inv})
        max_hold = cfg.time_stop_minutes
        spec = json.loads(s["spec_json"] or "{}")
        if s["source"] == GROK and spec["item"].get("max_hold_minutes"):
            max_hold = min(max_hold, int(spec["item"]["max_hold_minutes"]))
        # the time stop never runs past the day's forced flat (earlier on a half day)
        max_hold = max(min(max_hold, int((self._flat() - now).total_seconds() // 60)), 1)
        flags = self._muse_flags(now)
        ctx = {"muse_flag": flags.get(sym, ""), "halted": halted, "plan_id": s["plan_id"] or "",
               "plan_shadow_only": bool(spec.get("shadow_only", False)), "received_at": spec.get("received_at")}
        if s["source"] == GROK and spec["item"].get("preferred_dte") == "0dte":
            ctx["note"] = "plan prefers 0dte; live uses 1-3dte, 0dte is a shadow variant"
        band = cfg.delta_band(sym)
        live_choice = None
        opened = 0
        for bucket in cfg.dte_buckets:
            chain = self._chain(sym, bucket)
            if chain is None:
                ostore.filtered(self.con, d, now, "contract", sym, "chain_error", sid, bucket.label)
                continue
            choice, why, detail = sel.choose(chain, spot, now, sig.direction, bucket, band, cfg)
            if choice is None:
                ostore.filtered(self.con, d, now, "contract", sym, why, sid, bucket.label, detail)
                continue
            for ev in cfg.exit_variants:
                opened += self._open_shadow(s, sig, choice, ev, now, max_hold, ctx)
            if bucket.label == cfg.live_dte:
                live_choice = choice
        pending = False
        if s["live_eligible"]:
            why = self._live_block(s, sig, spec, regime)
            if why:
                ostore.filtered(self.con, d, now, "live", sym, why, sid,
                                detail={"day_rvol": sig.meta.get("day_rvol"), "rvol5": sig.meta.get("rvol5"),
                                        "macro": regime["macro_events"]})
            else:
                pending = self._open_live(s, sig, live_choice, now, halted, flags, max_hold, ctx)
        ostore.update_setup(self.con, sid, state=ENTERED if (opened or pending) else SKIPPED)

    def _grok_stance(self, s: dict, direction: str) -> tuple[str | None, int]:
        """(stance, late) of a RULE setup at its trigger, from the directional
        priors (items without a trigger) of the plan in force: 'agree' (same
        bias), 'disagree' (opposite), 'none' (no prior on the name, or priors
        that contradict each other). Recorded and reported only; it never
        changes live eligibility. Grok's own setups: (None, 0)."""
        if s["source"] == GROK:
            return None, 0
        a = self._active
        if a is None:
            return "none", 0
        biases = {i.bias for i in a.plan.plans if i.directional and i.underlying == s["underlying"]}
        if len(biases) != 1:
            return "none", int(a.shadow_only)
        return ("agree" if biases == {direction} else "disagree"), int(a.shadow_only)

    def _skip(self, s: dict, now: datetime, why: str, detail: dict | None = None) -> None:
        ostore.update_setup(self.con, s["setup_id"], state=SKIPPED)
        ostore.filtered(self.con, self._dstr, now, "entry", s["underlying"], why, s["setup_id"], detail=detail)

    def _muse_flags(self, now: datetime) -> dict[str, str]:
        if self._flags is None:
            try:
                self._flags = dict(self.flags_fn(now))
            except Exception as e:  # unknown flags: block every live entry this tick (reduce risk, never add)
                self._error("flags", str(e))
                self._flags = {"*": f"flag read failed: {e}"}
        return self._flags

    # ================================================================ entries
    def _open_shadow(self, s: dict, sig: pb.Signal, ch: sel.Choice, ev: str, now: datetime, max_hold: int,
                     ctx: dict) -> int:
        cfg, c = self.cfg, ch.contract
        price = sh.buy_price(c.ask, cfg)
        qty = sh.contracts_for(price, cfg)
        pid = f"{s['setup_id']}:shadow:{ch.dte_label}:{ev}"
        if qty < 1:
            ostore.filtered(self.con, self._dstr, now, "contract", s["underlying"], "premium_above_cap", s["setup_id"],
                            f"{ch.dte_label}|{ev}", {"ask": c.ask})
            return 0
        row = self._position_row(s, sig, ch, pid, "shadow", ev, max_hold, ctx)
        row.update(qty_initial=qty, qty_open=qty, state=ENTERED, entry_time=ostore.iso(now), entry_price=price,
                   fees_usd=cfg.fee_per_contract * qty, spread_cost_usd=sh.half_spread_cost(c.bid, c.ask, qty, cfg))
        if ostore.insert_position(self.con, row):
            ostore.add_fill(self.con, pid, now, "buy", qty, price, c.bid, c.ask, "entry")
        return 1

    def _position_row(self, s, sig, ch: sel.Choice, pid, mode, ev, max_hold, ctx) -> dict:
        c = ch.contract
        return {"position_id": pid, "setup_id": s["setup_id"], "day": self._dstr, "mode": mode, "source": s["source"],
                "underlying": s["underlying"], "uclass": s["uclass"], "dte_label": ch.dte_label,
                "band_label": ch.band_label, "occ": c.occ, "right": c.right, "strike": c.strike,
                "expiry": str(c.expiry), "entry_bid": c.bid, "entry_ask": c.ask,
                "entry_quote_json": json.dumps(ch.quote_dict()), "invalidation": sig.invalidation,
                "inv_close": sig.inv_close, "inv_vwap": int(sig.inv_vwap), "exit_variant": ev,
                "direction": sig.direction, "max_hold_min": max_hold, "tp_done": 0, "max_bid": c.bid,
                "min_bid": c.bid, "last_bid": c.bid, "last_ask": c.ask, "realized_usd": 0.0, "fees_usd": 0.0,
                "spread_cost_usd": 0.0, "flags_json": json.dumps(ctx, ensure_ascii=False, default=str)}

    def _open_live(self, s: dict, sig: pb.Signal, ch: sel.Choice | None, now: datetime, halted: bool,
                   flags: dict[str, str], max_hold: int, ctx: dict) -> bool:
        cfg, d, sym, sid = self.cfg, self._dstr, s["underlying"], s["setup_id"]

        def block(why: str, detail: dict | None = None) -> bool:
            ostore.filtered(self.con, d, now, "live", sym, why, sid, detail=detail)
            return False

        if halted:
            return block("halt_file")
        if sym in flags or "*" in flags:
            return block("muse_flag", {"flag": flags.get(sym) or flags.get("*")})
        if ch is None:
            return block("no_live_contract")
        if ch.contract.expiry <= self._day:  # never a live order on a contract that expires today
            return block("expires_today", {"occ": ch.contract.occ})
        left = (self._flat() - now).total_seconds() / 60
        if left < cfg.min_live_minutes_before_flat:  # e.g. after 11:15 ET on a 13:00 close
            return block("too_close_to_flat", {"minutes_left": round(left), "flat": f"{self._flat():%H:%M}"})
        # day <= today: a leftover of an earlier day still counts until it is sold
        n_live = self.con.execute("SELECT COUNT(*) FROM options_positions WHERE day<=? AND mode='live' AND state IN (?,?)",
                                  (d, PENDING, ENTERED)).fetchone()[0]
        if n_live >= cfg.max_open_positions:
            return block("max_open_positions")
        n_sym = self.con.execute("SELECT COUNT(*) FROM options_positions WHERE day<=? AND mode='live' AND underlying=? "
                                 "AND state IN (?,?)", (d, sym, PENDING, ENTERED)).fetchone()[0]
        if n_sym >= cfg.max_live_per_underlying:
            return block("max_live_per_underlying")
        c = ch.contract
        limit = sh.round_tick(c.ask + cfg.entry_limit_offset, sh.option_tick(sym, c.ask), up=True)
        qty = sh.contracts_for(limit, cfg)
        if qty < 1:
            return block("premium_above_cap", {"limit": limit})
        pid = f"{sid}:live:{ch.dte_label}:{cfg.live_exit}"
        intent = OrderIntent(ostore.cid(pid, "e"), c.occ, AssetClass.OPTION, Side.BUY, qty=qty,
                             order_type=OrderType.LIMIT, tif=TIF.DAY, limit_price=limit,
                             strategy=f"options:{s['source']}", reason=sig.reason[:200])
        try:
            self._io()
            dec = self.risk_gate.check(intent, c, now, marks=self._live_marks(), blocked=flags)
        except Exception as e:  # a failing gate is a "no"
            self._error("risk_gate", str(e))
            return block("risk_gate_error", {"error": str(e)[:200]})
        if not dec.allow:
            return block("risk_gate", {"reason": dec.reason})
        final = dec.intent or intent
        if (final.client_order_id != intent.client_order_id or final.symbol != intent.symbol
                or final.side != Side.BUY or final.qty is None or final.qty != int(final.qty)
                or not 1 <= final.qty <= qty or final.order_type != OrderType.LIMIT
                or final.limit_price is None or final.limit_price > limit):
            return block("risk_gate_bad_intent", {"reason": dec.reason})
        row = self._position_row(s, sig, ch, pid, "live", cfg.live_exit, max_hold, ctx)
        row.update(qty_initial=final.qty, qty_open=0, state=PENDING)
        if not ostore.insert_position(self.con, row):
            return True  # already exists (re-run): the pending order is resolved by _manage_pending
        self._submit(pid, final, "entry", "entry", now)
        # reserved even when the submit outcome is unknown: counting a buy
        # that never reached the broker only blocks entries, never adds risk
        self.risk_gate.reserve(final, final.limit_price)
        # resolve at once: a marketable limit usually fills on arrival, and an
        # unbooked fill would make the gate see a book / broker mismatch and
        # refuse the next entry in this same tick
        try:
            self._resolve_entry(ostore.position(self.con, pid), now)
        except Exception as e:
            self._error("entry_order", f"{pid}: {type(e).__name__}: {e}")
        return True

    def _live_marks(self) -> dict[str, float]:
        """OCC -> mid of the live positions' quotes that were fresh on THIS
        tick (the gate values the open book with them). A position without
        one gets no mark here, so the gate uses the broker's current_price
        (and refuses buys when that is missing too): a stored last bid / ask
        from minutes ago must never hide a loss from the daily stop."""
        out = {}
        for p in self._live_rows((ENTERED,)):
            q = self._quotes.get(p["occ"])
            if self._fresh(q, self._now):
                out[p["occ"]] = q.mid
        return out

    def _book(self, p: dict, cid: str, side: Side, o) -> bool:
        """Book a live fill (cumulative qty / average price) in line A's book.
        Called on every poll of a filled order (the book is idempotent per
        client_order_id), so a refused fill is retried. The store's own
        writes are committed first, so a book error can never discard them.
        False when the book refuses it: the caller keeps the order polled
        (not terminal), the position is flagged and the owner paged once."""
        if not (o.filled_qty or 0) > 0 or o.filled_avg_price is None:
            return True
        self._io()
        ok = self.risk_gate.book_fill(client_order_id=cid, occ=p["occ"], strategy=f"options:{p['source']}", side=side,
                                      qty=o.filled_qty, price=o.filled_avg_price, filled_at=o.filled_at or self._now)
        if ok is not False:
            return True
        if ostore.notify_once(self.con, f"book_refused:{cid}"):
            ostore.update_position(self.con, p["position_id"],
                                   note=((p["note"] or "") + f"; options book refused fill {cid}: reconcile needed")
                                   .strip("; ")[:500])
            ostore.event(self.con, self._dstr, self._now, "critical", "book_refused", f"{p['position_id']} {cid}")
            self._notify("期權軌道:期權帳本拒收成交", f"{p['occ']} 的成交 {cid}({side.value} {o.filled_qty:g})被 A 線"
                         "期權帳本拒收,帳本與券商將不一致(新進場會被擋)。該委託會持續重查;請核對 Alpaca 並修正帳本。",
                         notify.CRITICAL)
        return False

    def _submit(self, pid: str, intent: OrderIntent, kind: str, reason: str, now: datetime) -> None:
        """Commit the order row first, then submit. A second call for the same
        client_order_id never reaches the broker again."""
        if not ostore.record_order(self.con, intent.client_order_id, pid, kind, intent.side.value, intent.qty,
                                   intent.order_type.value, intent.limit_price, reason, now):
            return
        try:
            self._io()
            o = self.broker.submit(intent)
        except Exception as e:  # unknown outcome: stays pending_submit, resolved by get_order next tick
            ostore.update_order(self.con, intent.client_order_id, note=f"submit error: {type(e).__name__}: {e}"[:500])
            self._error("submit", f"{intent.client_order_id}: {e}")
        else:
            # never terminal here, even if the broker filled it at once: fills
            # are booked only by the resolution pass (get_order), next tick
            ostore.update_order(self.con, intent.client_order_id, status="submitted", note=f"ack {o.status.value}")
        self.con.commit()

    # ================================================================ live order resolution
    def _manage_pending(self, now: datetime) -> None:
        for p in self._live_rows((PENDING,)):
            try:
                self._resolve_entry(p, now)
                p = ostore.position(self.con, p["position_id"])
                if p["state"] == PENDING:
                    self._exit_partial(p, now)
            except Exception as e:
                self._error("entry_order", f"{p['position_id']}: {type(e).__name__}: {e}")

    def _pending_due(self, p: dict, now: datetime) -> str:
        """Exit reason for the filled part of an entry still working, or ''."""
        if p["day"] < self._dstr:
            return "carry_over" if _et(now) >= _at(self._day, self.cfg.open_time) else ""
        if now >= self._flat():
            return "force_flat"
        row = ostore.order(self.con, ostore.cid(p["position_id"], "e"))
        if row is not None:
            held = (now - datetime.fromisoformat(row["submitted_at"])).total_seconds() / 60
            if held >= (p["max_hold_min"] or self.cfg.time_stop_minutes):
                return "time_stop"
        return ""

    def _exit_partial(self, p: dict, now: datetime) -> None:
        """A partly filled entry whose rest is still working (its cancel has
        not taken effect, or keeps failing): once due, sell what filled at
        market. The row stays PENDING until the entry order ends; later
        fills of it are booked and sold the same way."""
        working = [o for o in ostore.orders_for(self.con, p["position_id"])
                   if o["kind"] == "exit" and o["status"] not in _TERMINAL_ORDER]
        if working:
            self._resolve_exit(p, working[-1], now)
            return
        if (p["qty_open"] or 0) <= 1e-9:
            return
        reason = self._pending_due(p, now)
        if reason:
            self._send_exit(p, p["qty_open"], reason, None, now)

    def _cancel(self, cid: str, why: str, page: bool = True) -> bool:
        """Request a cancel; a failure is counted and, after
        cancel_max_failures in a row, paged (CRITICAL) once per order
        (``page=False``: the caller escalates itself, see _cancel_exit)."""
        try:
            self._io()
            self.broker.cancel(cid)
        except Exception as e:
            n = self._cancel_fail[cid] = self._cancel_fail.get(cid, 0) + 1
            self._error("cancel", f"{cid}: {type(e).__name__}: {e}")
            if page and n >= self.cfg.cancel_max_failures and ostore.notify_once(self.con,
                                                                                 f"{self._dstr}:cancel_failed:{cid}"):
                ostore.event(self.con, self._dstr, self._now, "critical", "cancel_failed", f"{cid} x{n}: {e}")
                self._notify("期權軌道:撤單一直失敗", f"{cid} 已連續 {n} 次撤單失敗({type(e).__name__}: {e})。"
                             "已成交的部分會照常賣出;仍在掛的單請到 Alpaca 手動撤銷。", notify.CRITICAL)
            return False
        self._cancel_fail.pop(cid, None)
        ostore.update_order(self.con, cid, note=f"cancel requested ({why})")
        return True

    def _broker_order(self, row: dict, now: datetime):
        """(order or None, gone). ``gone`` = the broker never received it."""
        self._io()
        o = self.broker.get_order(row["client_order_id"])
        if o is None and row["status"] == ostore.PENDING_SUBMIT:
            age = (now - datetime.fromisoformat(row["submitted_at"])).total_seconds()
            if age > NOT_SENT_AFTER_S:
                ostore.update_order(self.con, row["client_order_id"], status="not_sent")
                return None, True
        return o, False

    def _resolve_entry(self, p: dict, now: datetime) -> None:
        cfg, pid = self.cfg, p["position_id"]
        ecid = ostore.cid(pid, "e")
        row = ostore.order(self.con, ecid)
        if row is None:  # crashed between the position and the order row: nothing was sent
            ostore.update_position(self.con, pid, state=ABANDONED, exit_reason="entry_not_sent", exit_time=ostore.iso(now))
            self.risk_gate.release(p["occ"])
            return
        o, gone = self._broker_order(row, now)
        if gone:
            ostore.update_position(self.con, pid, state=ABANDONED, exit_reason="entry_not_sent", exit_time=ostore.iso(now))
            self.risk_gate.release(p["occ"])
            return
        if o is None:
            return
        booked = row["booked_qty"] or 0
        delta = (o.filled_qty or 0) - booked
        if delta > 0 and o.filled_avg_price:
            # the new contracts at their own price (the broker reports a cumulative average), and
            # entry_price = average cost of the contracts still OPEN, so a sell after a partial
            # entry kept filling is measured against what those contracts cost (as line A's book)
            px = (o.filled_qty * o.filled_avg_price - (row["avg_price"] or 0.0) * booked) / delta
            if not (px > 0):
                px = o.filled_avg_price
            have = p["qty_open"] or 0
            cost = ((p["entry_price"] or 0.0) * have + px * delta) / (have + delta)
            ostore.add_fill(self.con, pid, now, "buy", delta, px, p["entry_bid"], p["entry_ask"],
                            "entry", f"{ecid}#{o.filled_qty:g}")
            ostore.update_position(self.con, pid, qty_open=have + delta, entry_price=cost,
                                   fees_usd=(p["fees_usd"] or 0) + cfg.fee_per_contract * delta,
                                   spread_cost_usd=(p["spread_cost_usd"] or 0)
                                   + sh.half_spread_cost(p["entry_bid"], p["entry_ask"], delta, cfg))
            ostore.update_order(self.con, ecid, booked_qty=o.filled_qty, filled_qty=o.filled_qty,
                                avg_price=o.filled_avg_price)
        if not self._book(p, ecid, Side.BUY, o):
            return  # refused by the book: not terminal, polled again next tick
        status = o.status.value
        ostore.update_order(self.con, ecid, status=status)
        if status in _TERMINAL_ORDER:
            self.risk_gate.release(p["occ"])
            cur = ostore.position(self.con, pid)
            if o.filled_qty and o.filled_qty > 0 and (cur["qty_open"] or 0) <= 1e-9 and cur["exit_reason"]:
                # every filled contract was already sold while the entry was still working
                ostore.update_position(self.con, pid, state=EXITED, qty_initial=o.filled_qty,
                                       entry_time=ostore.iso(o.filled_at or now),
                                       exit_time=cur["exit_time"] or ostore.iso(now))
            elif o.filled_qty and o.filled_qty > 0:
                ostore.update_position(self.con, pid, state=ENTERED, qty_initial=o.filled_qty,
                                       entry_time=ostore.iso(o.filled_at or now))
                self._notify("期權紙上進場", f"{p['underlying']} {p['occ']} x{o.filled_qty:g} @ {o.filled_avg_price:.2f}"
                             f"({p['source']})", notify.INFO)
            else:
                ostore.update_position(self.con, pid, state=ABANDONED, exit_reason="entry_unfilled",
                                       exit_time=ostore.iso(now))
            return
        age = (now - datetime.fromisoformat(row["submitted_at"])).total_seconds()
        if age >= cfg.entry_timeout_s or now >= self._flat():
            self._cancel(ecid, "entry timeout")

    # ================================================================ exits
    def _manage_open(self, now: datetime) -> None:
        rows = ostore.leftovers(self.con, self._dstr, "live", (ENTERED,)) + ostore.positions(self.con, self._dstr, None,
                                                                                               (ENTERED,))
        for p in rows:
            try:
                self._manage_one(p, now)
            except Exception as e:
                self._error("exit", f"{p['position_id']}: {type(e).__name__}: {e}")

    def _quote(self, occ: str) -> OptionContract | None:
        if occ not in self._quotes:
            try:
                self._io()
                self._quotes[occ] = self.broker.option_quote(occ)
            except Exception as e:
                self._error("quote", f"{occ}: {e}")
                self._quotes[occ] = None
        return self._quotes[occ]

    def _fresh(self, q: OptionContract | None, now: datetime) -> bool:
        """A usable quote: finite, sane two-sided (OptionContract.mid) and recent."""
        return (q is not None and sel.mid(q) is not None and q.as_of is not None
                and (now - q.as_of).total_seconds() <= self.cfg.max_quote_age_s)

    def _manage_one(self, p: dict, now: datetime) -> None:
        q = self._quote(p["occ"])
        fresh = self._fresh(q, now)
        if fresh:
            ostore.update_position(self.con, p["position_id"], last_bid=q.bid, last_ask=q.ask,
                                   max_bid=max(p["max_bid"] or q.bid, q.bid), min_bid=min(p["min_bid"] or q.bid, q.bid))
            p = ostore.position(self.con, p["position_id"])
        if p["tp_done"] and p["exit_variant"] == "B":  # variant B trails the best underlying price since +40%
            spot = self._spot(p["underlying"], now)
            ref = p["trail_ref"]
            if spot is not None and (ref is None or (spot > ref if p["direction"] == "long" else spot < ref)):
                ostore.update_position(self.con, p["position_id"], trail_ref=spot)
                p = ostore.position(self.con, p["position_id"])
        if p["mode"] == "live":
            orders = ostore.orders_for(self.con, p["position_id"])
            for o in orders:
                if o["kind"] == "exit" and o["status"] == UNRESOLVED:
                    self._poll_unresolved(p, o, now)
            p = ostore.position(self.con, p["position_id"])
            if p["state"] != ENTERED:
                return
            working = [o for o in orders if o["kind"] == "exit" and o["status"] not in _TERMINAL_ORDER]
            if working:
                self._resolve_exit(p, working[-1], now)
                return
        reason, qty = self._exit_decision(p, q if fresh else None, now)
        if not reason:
            return
        if p["mode"] == "shadow":
            bid = q.bid if fresh else p["last_bid"]
            ask = q.ask if fresh else p["last_ask"]
            if bid is None:
                return
            self._book_exit(p, qty, sh.sell_price(bid, self.cfg), bid, ask, reason, now,
                            "" if fresh else "stale quote: last known bid")
        else:  # a leftover always goes out at market
            self._send_exit(p, qty, reason, None if reason == "carry_over" else (q if fresh else None), now)

    def _exit_decision(self, p: dict, q: OptionContract | None, now: datetime) -> tuple[str, float]:
        cfg = self.cfg
        qty = p["qty_open"]
        if p["day"] < self._dstr:  # left over from an earlier session: out from the open, first tick
            return ("carry_over", qty) if _et(now) >= _at(self._day, cfg.open_time) else ("", 0)
        if now >= self._flat():
            return "force_flat", qty
        spot = self._spot(p["underlying"], now)
        inv, long_ = p["invalidation"], p["direction"] == "long"
        if spot is not None and inv is not None and (spot <= inv if long_ else spot >= inv):
            return "invalidation", qty
        if self._close_invalidated(p, now):
            return "invalidation", qty
        held = (now - datetime.fromisoformat(p["entry_time"])).total_seconds() / 60
        if held >= (p["max_hold_min"] or cfg.time_stop_minutes):
            return "time_stop", qty
        b = p["exit_variant"] == "B"
        if b and p["tp_done"] and spot is not None and p["trail_ref"] is not None:
            a = self._underlying_atr(p["underlying"], now)
            if a is not None:
                gap = cfg.trail_atr_mult * a
                if (spot <= p["trail_ref"] - gap) if long_ else (spot >= p["trail_ref"] + gap):
                    return "atr_trail", qty
        if q is None:
            return "", 0
        m, e = sel.mid(q), p["entry_price"]
        if not p["tp_done"]:
            if m <= e * (1 - (cfg.premium_stop_pct_b if b else cfg.premium_stop_pct)):
                return "premium_stop", qty
            if m >= e * (1 + cfg.take_profit_pct):
                part = floor(qty * cfg.take_profit_fraction) if qty >= 2 else qty
                return "take_profit", part
        elif not b and m <= (p["stop_price"] or e):
            return "breakeven_stop", qty
        return "", 0

    def _rth_now(self, sym: str, now: datetime) -> pd.DataFrame | None:
        """Today's completed regular-session bars, or None when the last one
        is older than max_bar_age_s (stale data decides nothing)."""
        done = pb.completed(self._bars_now(sym, now), now)
        if done.empty or done.index[-1].date() != self._day:
            return None
        _, rth = pb.split_session(done, self.cfg)
        if rth.empty or (now - (rth.index[-1] + pb.ONE_MIN).to_pydatetime()).total_seconds() > self.cfg.max_bar_age_s:
            return None
        return rth

    def _close_invalidated(self, p: dict, now: datetime) -> bool:
        """Close-based invalidation: the last completed 1-minute close (after
        the entry) beyond ``inv_close``, or back across session VWAP."""
        if p["inv_close"] is None and not p["inv_vwap"]:
            return False
        rth = self._rth_now(p["underlying"], now)
        if rth is None or (rth.index[-1] + pb.ONE_MIN).to_pydatetime() <= datetime.fromisoformat(p["entry_time"]):
            return False
        c, long_ = float(rth["close"].iloc[-1]), p["direction"] == "long"
        if p["inv_close"] is not None and (c < p["inv_close"] if long_ else c > p["inv_close"]):
            return True
        if p["inv_vwap"]:
            v = float(pb.session_vwap(rth).iloc[-1])
            if v == v and (c < v if long_ else c > v):  # v == v: not NaN
                return True
        return False

    def _underlying_atr(self, sym: str, now: datetime) -> float | None:
        rth = self._rth_now(sym, now)
        return None if rth is None else pb.atr(rth, self.cfg.atr_period)

    def _spot(self, sym: str, now: datetime) -> float | None:
        if sym not in self._spots:
            v = None
            try:
                self._io()
                q = self.broker.stock_quote(sym)
                if q and q[0] > 0 and q[1] >= q[0]:
                    v = (q[0] + q[1]) / 2
            except Exception as e:
                self._error("stock_quote", f"{sym}: {e}")
            if v is None:  # fall back to a fresh completed bar
                b = pb.completed(self._bars.get(sym), now)
                if not b.empty and (now - (b.index[-1] + pb.ONE_MIN).to_pydatetime()).total_seconds() <= self.cfg.max_bar_age_s:
                    v = float(b["close"].iloc[-1])
            self._spots[sym] = v
        return self._spots[sym]

    def _book_exit(self, p: dict, qty: float, price: float, bid, ask, reason: str, now: datetime,
                   note: str = "", client_order_id: str = "") -> None:
        cfg, pid = self.cfg, p["position_id"]
        if not ostore.add_fill(self.con, pid, now, "sell", qty, price, bid, ask, reason, client_order_id):
            return  # already booked (re-run)
        left = p["qty_open"] - qty
        fields = {"qty_open": left,
                  "realized_usd": (p["realized_usd"] or 0) + (price - p["entry_price"]) * qty * cfg.contract_multiplier,
                  "fees_usd": (p["fees_usd"] or 0) + cfg.fee_per_contract * qty,
                  "spread_cost_usd": (p["spread_cost_usd"] or 0) + sh.half_spread_cost(bid, ask, qty, cfg)}
        if reason == "take_profit" and left > 0:
            fields.update(tp_done=1, stop_price=p["entry_price"], trail_ref=self._spot(p["underlying"], now))
        if left <= 1e-9:
            fields.update(exit_time=ostore.iso(now), exit_reason=reason)
            if p["state"] != PENDING:  # a partly filled entry still working stays PENDING until it ends
                fields["state"] = EXITED
        if note:
            fields["note"] = ((p["note"] or "") + "; " + note).strip("; ")[:500]
        ostore.update_position(self.con, pid, **fields)

    def _send_exit(self, p: dict, qty: float, reason: str, q: OptionContract | None, now: datetime,
                   force_market: bool = False, allow_reconcile: bool = True) -> bool:
        """Send one sell to close through the risk gate. A limit the gate
        refuses (price band, no sane quote) is retried as a market sell; a
        market sell the gate refuses (nothing held at the broker: it would
        open a short) is not sent, and the owner is paged once."""
        cfg, pid = self.cfg, p["position_id"]
        exits = [o for o in ostore.orders_for(self.con, pid) if o["kind"] == "exit"]
        streak = 0
        for o in reversed(exits):
            if (o["filled_qty"] or 0) > 0:
                break
            streak += 1
        if exits and now >= self._flat():  # at/after the forced flat a repeat attempt is a market sell
            force_market = True
        if not force_market and q is not None and q.bid and streak < cfg.exit_max_limit_attempts:
            otype, limit = OrderType.LIMIT, sh.round_tick(q.bid, sh.option_tick(p["underlying"], q.bid), up=False)
        else:
            otype, limit = OrderType.MARKET, None
        # size the sell to what the broker holds minus its working sells (never more than asked)
        room = self._sell_room(p["occ"])
        sized = None
        if room is not None:
            held, working = room
            # never while one of our own exits is unresolved or still open: its unbooked fills
            # would look like contracts "gone at the broker"
            own_open = any(o["kind"] == "exit" and (o["status"] == UNRESOLVED or o["status"] not in _TERMINAL_ORDER)
                           for o in exits)
            if (allow_reconcile and not own_open and working <= 1e-9 and 1 <= held < (p["qty_open"] or 0) - 1e-9
                    and self._settled(p, now)):
                p = self._reconcile_qty(p, held, now)  # someone else took contracts: store / book follow
            can = floor(min(qty, held - working, p["qty_open"] or 0) + 1e-9)
            if can < 1:
                sized = RiskDecision(False, f"nothing to sell: broker holds {held:g}, working sells {working:g}")
            qty = max(can, 0)
        intent = OrderIntent(ostore.cid(pid, f"x{len(exits)}"), p["occ"], AssetClass.OPTION, Side.SELL,
                             qty=max(qty, 1), order_type=otype, tif=TIF.DAY, limit_price=limit,
                             strategy=f"options:{p['source']}", reason=reason)
        if sized is not None:
            dec = sized
        else:
            dec = self._exit_gate(intent, q, now)
            if not dec.allow and intent.order_type == OrderType.LIMIT:
                intent = replace(intent, order_type=OrderType.MARKET, limit_price=None)
                dec = self._exit_gate(intent, q, now)
        if not dec.allow and p["day"] < self._dstr and p["state"] == ENTERED and self._broker_flat(p["occ"]):
            # a leftover the broker no longer holds (closed by hand, expired, exercised):
            # reconcile it, never sell
            self._reconcile_flat(p, "broker_flat_unreconciled", now, dec.reason)
            return False
        if not dec.allow:
            log.warning("options exit refused by the risk gate: %s %s", pid, dec.reason)
            if ostore.notify_once(self.con, f"{self._dstr}:exit_refused:{pid}"):
                ostore.event(self.con, self._dstr, now, "critical", "exit_refused", f"{pid} {p['occ']}: {dec.reason}")
                self._notify("期權軌道:出場被風控擋下", f"{p['occ']} x{qty:g}({reason})被風控拒絕:{dec.reason[:300]}。"
                             "每輪會重試;請到 Alpaca 確認實際持倉。", notify.CRITICAL)
            return False
        self._submit(pid, dec.intent or intent, "exit", reason, now)
        return True

    def _sell_room(self, occ: str) -> tuple[float, float] | None:
        """(held, working sells) of ``occ`` from a fresh broker read; None
        when the broker has no such calls or a read fails (the gate, which
        reads the same, then decides)."""
        if not hasattr(self.broker, "account"):
            return None
        held = self._broker_held(occ)
        if held is None:
            return None
        working = 0.0
        if hasattr(self.broker, "open_orders"):
            self._io()
            try:
                working = sum((o.qty or 0) - (o.filled_qty or 0) for o in self.broker.open_orders()
                              if o.symbol == occ and o.side == Side.SELL)
            except Exception as e:
                self._error("open_orders", f"{type(e).__name__}: {e}")
                return None
        return float(held), float(working)

    def _settled(self, p: dict, now: datetime, seconds: float = 60.0) -> bool:
        """No fill of this position in the last ``seconds``: a broker position
        read right after a fill can lag, and must not be taken as a loss."""
        last = self.con.execute("SELECT MAX(at) FROM options_fills WHERE position_id=?", (p["position_id"],)).fetchone()[0]
        return last is None or (now - datetime.fromisoformat(last)).total_seconds() >= seconds

    def _reconcile_qty(self, p: dict, held: float, now: datetime) -> dict:
        """The broker holds fewer contracts than the store (the owner sold
        some by hand, or one was assigned): the store row and the options
        book come down to ``held`` (the missing contracts' P&L is unknown and
        noted), and the owner is paged once. Never sends an order."""
        gone = (p["qty_open"] or 0) - held
        ostore.update_position(self.con, p["position_id"], qty_open=held,
                               note=((p["note"] or "") + f"; {gone:g} contracts gone at the broker at "
                                     f"{ostore.iso(now)}: their P&L is unknown").strip("; ")[:500])
        try:
            if self._obook is None:
                self._obook = OptionsBook(self.con)
            self._obook.reconcile_flat(p["occ"], "broker_qty_short", now, keep=held)
        except Exception as e:
            self._error("options_book", f"reconcile {p['occ']}: {type(e).__name__}: {e}")
        ostore.event(self.con, self._dstr, now, "critical", "broker_qty_short",
                     f"{p['position_id']} {p['occ']}: store {p['qty_open']:g}, broker {held:g}")
        self.con.commit()
        if ostore.notify_once(self.con, f"broker_qty_short:{p['position_id']}"):
            self._notify("期權軌道:券商口數與資料庫不符", f"{p['occ']} 資料庫記為 {p['qty_open']:g} 口,券商只有 {held:g} 口"
                         "(有人手動賣出,或被指派?)。只會賣出券商實際持有的口數;少掉的口數損益未知,請到 Alpaca 核對。",
                         notify.CRITICAL)
        return ostore.position(self.con, p["position_id"])

    def _exit_gate(self, intent: OrderIntent, q: OptionContract | None, now: datetime) -> RiskDecision:
        """The risk gate's verdict on one sell. A gate error is a refusal,
        and so is an allowed intent the gate changed (exits are never resized)."""
        try:
            self._io()
            dec = self.risk_gate.check_exit(intent, q, now)
        except Exception as e:
            self._error("risk_gate", f"exit {intent.client_order_id}: {type(e).__name__}: {e}")
            return RiskDecision(False, f"risk gate error: {type(e).__name__}: {e}")
        final = dec.intent or intent
        if dec.allow and ((final.client_order_id, final.symbol, final.side, final.order_type, final.limit_price)
                          != (intent.client_order_id, intent.symbol, Side.SELL, intent.order_type, intent.limit_price)
                          or final.qty is None or final.qty != int(final.qty) or not 1 <= final.qty <= intent.qty):
            # a gate may only lower a sell, never raise it or change anything else
            return RiskDecision(False, f"risk gate changed the exit: {dec.reason}")
        return RiskDecision(dec.allow, dec.reason, final if dec.allow else None)

    def _book_exit_order(self, p: dict, row: dict, o) -> tuple[dict, bool]:
        """Book the new part of an exit order's fills. The broker reports the
        CUMULATIVE filled qty and average price; the new contracts are booked
        at their own price, (cum_qty * cum_avg - value already booked) /
        new qty, so a partly filled exit never re-prices earlier contracts.
        Returns (the re-read position, whether line A's book took the fill)."""
        cid = row["client_order_id"]
        booked = row["booked_qty"] or 0
        delta = (o.filled_qty or 0) - booked
        if delta > 1e-9 and o.filled_avg_price is not None:
            done_value = (row["avg_price"] or 0.0) * booked
            price = (o.filled_qty * o.filled_avg_price - done_value) / delta
            self._book_exit(p, delta, price, p["last_bid"], p["last_ask"], row["reason"], self._now,
                            client_order_id=f"{cid}#{o.filled_qty:g}")
            ostore.update_order(self.con, cid, booked_qty=o.filled_qty, filled_qty=o.filled_qty,
                                avg_price=o.filled_avg_price)
            p = ostore.position(self.con, p["position_id"])
            if p["state"] == EXITED:
                self._notify("期權紙上出場", f"{p['underlying']} {p['occ']}:{p['exit_reason']},"
                             f"損益 {p['realized_usd'] - p['fees_usd']:+.0f} 美元", notify.INFO)
        return p, self._book(p, cid, Side.SELL, o)

    def _resolve_exit(self, p: dict, row: dict, now: datetime) -> None:
        cid = row["client_order_id"]
        try:
            o, gone = self._broker_order(row, now)
        except Exception as e:
            self._exit_status_failed(p, row, e, now)
            return
        self._status_fail.pop(cid, None)
        if o is None:
            return
        p, booked = self._book_exit_order(p, row, o)
        if not booked:
            return  # refused by the book: not terminal, polled again next tick
        ostore.update_order(self.con, cid, status=o.status.value)
        if o.status.value in _TERMINAL_ORDER:
            # at/after the forced flat the remainder goes out in this same call: the next
            # tick may never come (last tick of the session, flatten_now)
            if now >= self._flat() and p["state"] in (ENTERED, PENDING) and (p["qty_open"] or 0) > 1e-9:
                self._send_exit(p, p["qty_open"], "force_flat", None, now, force_market=True)
            return
        age = (now - datetime.fromisoformat(row["submitted_at"])).total_seconds()
        if age >= self.cfg.exit_timeout_s:
            self._cancel_exit(p, row, now, "reprice")
        elif now >= self._flat() and row["order_type"] == OrderType.LIMIT.value:
            # at/after the forced flat a working limit is replaced at once by one market sell
            self._cancel_exit(p, row, now, "forced flat: replace by a market sell")

    def _cancel_exit(self, p: dict, row: dict, now: datetime, why: str) -> None:
        """Cancel a working exit so it can be replaced. Failed cancels are
        retried with backoff (2, 4, 8 ... 60 s). After cancel_max_failures a
        fresh read of the broker's open orders decides: the order is gone
        there -> its row becomes 'unresolved' (still polled for late fills)
        and the rest is sold at market in this call; still working (or
        unreadable) -> NO second sell (it could double the sale), the owner
        is paged CRITICAL every cancel_page_every_min until it resolves."""
        cid, cfg = row["client_order_id"], self.cfg
        nxt = self._cancel_next.get(cid)
        if nxt is not None and now < nxt:
            return
        if self._cancel(cid, why, page=False):
            self._cancel_next.pop(cid, None)
            return
        n = self._cancel_fail.get(cid, 0)
        self._cancel_next[cid] = now + timedelta(seconds=min(2 ** n, 60))
        if n < cfg.cancel_max_failures:
            return
        working = self._broker_working(cid)
        if working is False:
            ostore.update_order(self.con, cid, status=UNRESOLVED, note="cancel failed, gone from the broker's open orders")
            p = ostore.position(self.con, p["position_id"])
            if (p["qty_open"] or 0) > 1e-9:
                self._send_exit(p, p["qty_open"], "force_flat" if now >= self._flat() else row["reason"], None, now,
                                force_market=True, allow_reconcile=False)
            return
        last = self._cancel_paged.get(cid)
        if last is None or (now - last).total_seconds() >= cfg.cancel_page_every_min * 60:
            self._cancel_paged[cid] = now
            state = "仍在券商掛著" if working else "狀態讀不到"
            ostore.event(self.con, self._dstr, now, "critical", "exit_cancel_failed", f"{cid} x{n}: {state}")
            self._notify("期權軌道:出場單撤不掉", f"{p['occ']} 的出場單 {cid} 已連續 {n} 次撤單失敗,{state}。"
                         f"為避免重複賣出不會再送第二張賣單;仍持有 {p['qty_open'] or 0:g} 口。請立即到 Alpaca "
                         f"手動撤單並平倉。每 {cfg.cancel_page_every_min:g} 分鐘提醒一次。", notify.CRITICAL)

    def _broker_working(self, cid: str) -> bool | None:
        """Is ``cid`` among the broker's open orders? None when unreadable."""
        if not hasattr(self.broker, "open_orders"):
            return None
        self._io()
        try:
            return any(o.client_order_id == cid for o in self.broker.open_orders())
        except Exception as e:
            self._error("open_orders", f"{type(e).__name__}: {e}")
            return None

    def _exit_status_failed(self, p: dict, row: dict, err: Exception, now: datetime) -> None:
        """get_order of a working exit failed. After order_status_max_failures
        in a row: page once, cancel it (best effort) and, only when a fresh
        broker read shows contracts still held, send ONE fresh market sell
        for min(held, open qty). The risk gate counts the broker's working
        sells, so a sell is never doubled while the old order still works.
        The old row becomes 'unresolved' and is still polled for late fills."""
        cid = row["client_order_id"]
        n = self._status_fail[cid] = self._status_fail.get(cid, 0) + 1
        self._error("exit_order", f"{cid}: get_order failed ({n}x): {type(err).__name__}: {err}")
        if n < self.cfg.order_status_max_failures:
            return
        self._status_fail[cid] = 0  # the next fallback only after another full run of failures
        if ostore.notify_once(self.con, f"{self._dstr}:exit_status:{p['position_id']}"):
            self._notify("期權軌道:出場單狀態讀不到", f"{p['occ']} 的出場單 {cid} 連續 {n} 次查不到狀態({err})。"
                         "會先撤舊單,再依券商持倉數量改送一張市價賣單(不會重複賣出)。", notify.CRITICAL)
        self._cancel(cid, "status unreadable")
        held = self._broker_held(p["occ"])
        if held is None or held <= 1e-9:
            return  # unknown, or nothing left at the broker: keep polling the old order
        qty = min(float(p["qty_open"] or 0), float(held))
        if qty < 1:
            return
        ostore.update_order(self.con, cid, status=UNRESOLVED, note="status unreadable; superseded by a market sell")
        if not self._send_exit(p, qty, row["reason"], None, now, force_market=True, allow_reconcile=False):
            ostore.update_order(self.con, cid, status=row["status"], note="status unreadable; fallback refused")

    def _poll_unresolved(self, p: dict, row: dict, now: datetime) -> None:
        """Book late fills of a superseded exit; a failure here is silent
        (it was paged when the row was superseded)."""
        try:
            self._io()
            o = self.broker.get_order(row["client_order_id"])
        except Exception as e:
            log.info("options unresolved exit %s still unreadable: %s", row["client_order_id"], e)
            return
        if o is None:
            return
        self._book_exit_order(p, row, o)
        if o.status.value in _TERMINAL_ORDER:
            ostore.update_order(self.con, row["client_order_id"], note=f"late status {o.status.value}")
        else:
            self._cancel(row["client_order_id"], "superseded exit still working")

    def _broker_flat(self, occ: str) -> bool:
        """True only when a fresh broker read shows 0 contracts of ``occ`` AND
        no working order on it (any read failure: False, nothing is assumed)."""
        held = self._broker_held(occ)
        if held is None or abs(held) > 1e-9:
            return False
        if hasattr(self.broker, "open_orders"):
            self._io()
            try:
                if any(o.symbol == occ for o in self.broker.open_orders()):
                    return False
            except Exception as e:
                self._error("open_orders", f"{type(e).__name__}: {e}")
                return False
        return not any(o["status"] not in _TERMINAL_ORDER
                       for q in self.con.execute("SELECT position_id FROM options_positions WHERE occ=? AND mode='live'",
                                                 (occ,)).fetchall()
                       for o in ostore.orders_for(self.con, q[0]))

    def _reconcile_flat(self, p: dict, reason: str, now: datetime, why: str = "") -> None:
        """Close a live row the broker does not hold: EXITED with ``reason``
        (its P&L is unknown and the report says so), the options book
        position removed by an explicit reconciliation row, owner paged once.
        This path only writes the store; it can never send an order."""
        pid = p["position_id"]
        ostore.update_position(self.con, pid, state=EXITED, qty_open=0, exit_time=ostore.iso(now), exit_reason=reason,
                               note=((p["note"] or "") + f"; broker holds 0 at {ostore.iso(now)}: P&L of "
                                     f"{p['qty_open'] or 0:g} contracts unknown").strip("; ")[:500])
        try:
            if self._obook is None:
                self._obook = OptionsBook(self.con)
            self._obook.reconcile_flat(p["occ"], reason, now)
        except Exception as e:
            self._error("options_book", f"reconcile {p['occ']}: {type(e).__name__}: {e}")
        ostore.event(self.con, self._dstr, now, "critical", reason, f"{pid} {p['occ']} x{p['qty_open'] or 0:g}: {why}"[:900])
        self.con.commit()
        if ostore.notify_once(self.con, f"{reason}:{pid}"):
            self._notify("期權軌道:部位已不在券商", f"{p['occ']} x{p['qty_open'] or 0:g}({p['day']})資料庫記為持有,"
                         "但券商持倉為 0 且沒有掛單(手動平倉、到期或被履約?)。已標記為已出場、損益未知,並從期權帳本"
                         "移除;沒有送出任何單。請到 Alpaca 核對實際損益。", notify.CRITICAL)

    def resolve_manual(self, position_id: str) -> str:
        """run.py resolve: close one live row by hand after the owner checked
        the broker. Refused unless a fresh broker read shows 0 contracts and
        no working order on the contract. Never sends an order."""
        now = self.clock()
        self._begin(now)
        p = ostore.position(self.con, position_id)
        if p is None or p["mode"] != "live" or p["state"] not in (PENDING, ENTERED):
            return f"refused: {position_id} is not an open live position"
        if not self._broker_flat(p["occ"]):
            return (f"refused: the broker still holds {p['occ']}, has a working order on it, or cannot be read; "
                    "close it at the broker first")
        self._reconcile_flat(p, "manual_resolved", now, "resolved by hand (run.py resolve)")
        return f"resolved: {position_id} {p['occ']} marked EXITED (manual_resolved), options book reconciled"

    def _broker_held(self, occ: str) -> float | None:
        """Contracts of ``occ`` the broker holds now, or None when unreadable."""
        try:
            self._io()
            snap = self.broker.account()
        except Exception as e:
            self._error("broker_positions", f"{type(e).__name__}: {e}")
            return None
        return sum(bp.qty for bp in snap.positions if bp.symbol == occ and bp.asset_class == AssetClass.OPTION)

    # ================================================================ leftovers and flatness
    def _start_of_day(self, now: datetime) -> None:
        """Once per day (first tick, or recover): close shadow leftovers of
        earlier days at their last known bid, page live leftovers (they are
        sold at market from the open by _manage_pending / _manage_open), and
        compare the broker's option positions with the store."""
        if self._swept == self._dstr:
            return
        self._swept = self._dstr
        for p in ostore.leftovers(self.con, self._dstr, "shadow", (ENTERED,)):
            self._book_exit(p, p["qty_open"], sh.sell_price(p["last_bid"] or 0.0, self.cfg), p["last_bid"],
                            p["last_ask"], "carry_over", now, "its session ended before the forced flat: last known bid")
        left = ostore.leftovers(self.con, self._dstr, "live", (PENDING, ENTERED))
        if left and ostore.notify_once(self.con, f"{self._dstr}:leftovers"):
            what = "; ".join(f"{p['occ']} x{p['qty_open'] or 0:g} ({p['state']}, {p['day']})" for p in left)
            ostore.event(self.con, self._dstr, now, "critical", "leftovers", what)
            self._notify("期權軌道:前一日部位未平", f"發現前一交易日未平倉的紙上期權部位:{what[:1200]}。"
                         f"{self.cfg.open_time[0]:02d}:{self.cfg.open_time[1]:02d} ET 起以市價賣出平倉。",
                         notify.CRITICAL)
        self._check_broker(now)
        self.con.commit()

    def _book_positions(self) -> set[str]:
        try:
            if self._obook is None:
                self._obook = OptionsBook(self.con)
            return set(self._obook.positions())
        except Exception as e:
            self._error("options_book", f"{type(e).__name__}: {e}")
            return set()

    def _check_broker(self, now: datetime) -> list[str]:
        """Broker option positions the store does not know (no live PENDING /
        ENTERED row on that contract): paged CRITICAL once per day and set,
        never sold (it may be the owner's own trade). The risk gate then
        refuses every entry (book / broker mismatch) until it is resolved."""
        if not hasattr(self.broker, "account"):
            return []
        try:
            self._io()
            snap = self.broker.account()
        except Exception as e:
            self._error("broker_positions", f"{type(e).__name__}: {e}")
            return []
        known = {r[0] for r in self.con.execute("SELECT DISTINCT occ FROM options_positions WHERE mode='live' "
                                                "AND state IN (?,?)", (PENDING, ENTERED))}
        book = self._book_positions()
        out = [f"{bp.symbol} x{bp.qty:g}" + (" (in the options book, not in the sleeve store)" if bp.symbol in book
                                             else " (unknown to the sleeve store and the options book)")
               for bp in snap.positions
               if bp.asset_class == AssetClass.OPTION and abs(bp.qty) > 1e-9 and bp.symbol not in known]
        if out and ostore.notify_once(self.con, f"{self._dstr}:unknown_broker:{'|'.join(sorted(out))}"[:400]):
            ostore.event(self.con, self._dstr, now, "critical", "unknown_broker_positions", "; ".join(out))
            self._notify("期權軌道:券商有未知期權部位", "券商持有本軌道不知道的期權部位(手動下單?):" + "; ".join(out)[:1200]
                         + "。本軌道不會自動賣出;帳本與券商不一致時當天新進場會被風控擋。請手動處理。", notify.CRITICAL)
        return out

    def _flat_problems(self) -> list[str]:
        """Live rows still PENDING / ENTERED (any day) and every option
        position the broker holds; empty when flat."""
        problems = [f"{p['occ']} ({p['state']}, {p['day']})" for p in self._live_rows((PENDING, ENTERED))]
        if hasattr(self.broker, "account"):
            try:
                self._io()
                snap = self.broker.account()
                problems += [f"broker {bp.symbol} x{bp.qty:g}" for bp in snap.positions
                             if bp.asset_class == AssetClass.OPTION and abs(bp.qty) > 1e-9]
            except Exception as e:
                problems.append(f"cannot read broker positions: {e}")
        return problems

    def _flat_alert(self, now: datetime) -> None:
        """Once a day, not_flat_alert_min before the close (15:50 ET, 12:50 on
        a 13:00 close): page CRITICAL if anything is still open, while there
        is still time to act by hand."""
        if now < self._close() - timedelta(minutes=self.cfg.not_flat_alert_min):
            return
        if not ostore.notify_once(self.con, f"{self._dstr}:flat_check"):
            return
        problems = self._flat_problems()
        if problems:
            ostore.event(self.con, self._dstr, now, "critical", "not_flat_before_close", "; ".join(problems))
            self._notify("期權軌道收盤前未平倉", f"距收盤 {self.cfg.not_flat_alert_min} 分鐘仍有期權部位未平:"
                         + "; ".join(problems)[:1500] + "。請立即到 Alpaca 確認。", notify.CRITICAL)

    # ================================================================ recover (run.py recover)
    def recover(self) -> dict:
        """Standalone leftover cleanup, no session: resolve every live row
        still PENDING / ENTERED (earlier days, and today's when no session
        is running: the caller holds the session lock), and while the market
        is open sell them at market through the risk gate. Pages unknown
        broker positions. Returns {"open": n rows still open, "market_open": bool}."""
        now = self.clock()
        self._begin(now)
        self._start_of_day(now)
        et = _et(now)
        market_open = is_trading_day(self._day) and _at(self._day, self.cfg.open_time) <= et < self._close()
        for p in self._live_rows((PENDING, ENTERED)):
            try:
                if p["state"] == PENDING:
                    self._resolve_entry(p, now)
                    p = ostore.position(self.con, p["position_id"])
                    if p["state"] not in (PENDING, ENTERED):
                        continue
                orders = ostore.orders_for(self.con, p["position_id"])
                working = [o for o in orders if o["kind"] == "exit" and o["status"] not in _TERMINAL_ORDER]
                if working:
                    self._resolve_exit(p, working[-1], now)
                elif market_open and (p["qty_open"] or 0) > 1e-9:
                    self._send_exit(p, p["qty_open"], "carry_over" if p["day"] < self._dstr else "recover_flat",
                                    None, now)
            except Exception as e:
                self._error("recover", f"{p['position_id']}: {type(e).__name__}: {e}")
        self._roll_setups()
        self.con.commit()
        return {"open": len(self._live_rows((PENDING, ENTERED))), "market_open": market_open}

    # ================================================================ wrap-up
    def _roll_setups(self) -> None:
        # day <= today: a leftover's setup is closed once the leftover is sold
        for s in [dict(r) for r in self.con.execute("SELECT setup_id FROM options_setups WHERE state=? AND day<=?",
                                                    (ENTERED, self._dstr))]:
            open_ = self.con.execute("SELECT COUNT(*) FROM options_positions WHERE setup_id=? AND state IN (?,?)",
                                     (s["setup_id"], PENDING, ENTERED)).fetchone()[0]
            if open_ == 0:
                ostore.update_setup(self.con, s["setup_id"], state=EXITED)

    def finish(self) -> bool:
        """End of session: close any shadow position still open at its last
        known bid, then verify the live book is flat (store AND broker, any
        day). Returns True when flat; pages the owner otherwise."""
        now = self.clock()
        self._begin(now)
        for p in ostore.positions(self.con, self._dstr, "shadow", (ENTERED,)):
            self._book_exit(p, p["qty_open"], sh.sell_price(p["last_bid"] or 0.0, self.cfg), p["last_bid"],
                            p["last_ask"], "session_end", now, "closed at last known bid")
        self._roll_setups()
        problems = self._flat_problems()
        self.con.commit()
        if problems:
            ostore.event(self.con, self._dstr, now, "critical", "not_flat", "; ".join(problems))
            self.con.commit()
            self._notify("期權軌道收盤未平倉", "收盤檢查發現期權部位未平:" + "; ".join(problems)[:1500], notify.CRITICAL)
            return False
        return True

    def flatten_now(self, reason: str = "force_flat") -> None:
        """Emergency path: treat every open position as due for the forced
        exit. A live position whose exit is still working is resolved, not
        sold a second time."""
        now = self.clock()
        self._begin(now)
        rows = self._live_rows((ENTERED,)) + ostore.positions(self.con, self._dstr, "shadow", (ENTERED,))
        for p in rows:
            try:
                q = self._quote(p["occ"])
                fresh = self._fresh(q, now)
                if p["mode"] == "shadow":
                    bid = q.bid if fresh else p["last_bid"]
                    if bid is not None:
                        self._book_exit(p, p["qty_open"], sh.sell_price(bid, self.cfg), bid,
                                        q.ask if fresh else p["last_ask"], reason, now)
                    continue
                working = [o for o in ostore.orders_for(self.con, p["position_id"])
                           if o["kind"] == "exit" and o["status"] not in _TERMINAL_ORDER]
                if working:
                    self._resolve_exit(p, working[-1], now)
                else:
                    self._send_exit(p, p["qty_open"], reason, q if fresh else None, now)
            except Exception as e:
                self._error("flatten", f"{p['position_id']}: {type(e).__name__}: {e}")
        self.con.commit()

    # ================================================================ plumbing
    def _io(self) -> None:
        """Commit before every broker / network call (and notification), so
        the write lock on the shared desk.sqlite is never held across I/O:
        the stock jobs wait at most 30 s for it (store.connect). Every write
        here is idempotent per deterministic id, so a commit mid-tick is safe."""
        if self.con.in_transaction:
            self.con.commit()

    def _bars_now(self, sym: str, now: datetime) -> pd.DataFrame:
        """The symbol's bars, refreshed at most once per tick."""
        if sym not in self._tick_bars:
            self._tick_bars[sym] = self._refresh_bars(sym, now)
        return self._tick_bars[sym]

    def _profile(self, sym: str, now: datetime) -> pb.VolProfile | None:
        """Volume profile of the previous sessions (same feed), built once a
        day and stored (options_profiles). A failed fetch is retried at most
        every 5 minutes; until then RVOL is unknown (shadow-only)."""
        if sym in self._profiles:
            return self._profiles[sym]
        got = ostore.profile(self.con, self._dstr, sym)
        if got is not None:
            self._profiles[sym] = pb.VolProfile.from_json(got[0])
            return self._profiles[sym]
        retry = self._profile_retry.get(sym)
        if retry is not None and now < retry:
            return None
        days, d = [], self._day
        while len(days) < self.cfg.rvol_lookback + 1:
            d = _prev_trading_day(d)
            days.append(d)
        try:
            self._io()
            hist = self.broker.minute_bars(sym, _at(days[-1], (4, 0)), _at(days[0], (16, 0)))
        except Exception as e:
            self._error("profile", f"{sym}: {type(e).__name__}: {e}")
            self._profile_retry[sym] = now + timedelta(minutes=5)
            return None
        prof = pb.build_profile(hist, self._day, self.cfg)
        note = "" if prof.sessions >= self.cfg.rvol_min_sessions else f"only {prof.sessions} sessions: RVOL unknown"
        ostore.save_profile(self.con, self._dstr, sym, prof.to_json(), note)
        if note:
            ostore.event(self.con, self._dstr, now, "warn", "profile", f"{sym}: {note}")
        self._profiles[sym] = prof
        return prof

    def _refresh_bars(self, sym: str, now: datetime) -> pd.DataFrame:
        have = self._bars.get(sym)
        day_start = _at(self._day, (4, 0))
        if have is None or have.empty or have.index[-1].date() != self._day:
            start, have = day_start, None
        else:
            start = (have.index[-1] - pd.Timedelta(minutes=3)).to_pydatetime()
        self._io()
        new = self.broker.minute_bars(sym, start, now)
        if new is not None and not new.empty:
            new = new.copy()
            new.index = pd.DatetimeIndex(new.index).tz_convert(ET)
        if have is not None:
            keep = have[have.index < pd.Timestamp(start)]
            merged = pd.concat([keep, new]) if new is not None and not new.empty else have
        else:
            merged = new if new is not None else pd.DataFrame()
        if not merged.empty:
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        self._bars[sym] = merged
        return merged

    def _chain(self, sym: str, bucket) -> list[OptionContract] | None:
        key = (sym, bucket.label)
        if key not in self._chains:
            lo, hi = sel.bucket_range(self._day, bucket)
            try:
                self._io()
                self._chains[key] = list(self.broker.option_chain(sym, lo, hi))
            except Exception as e:
                self._error("chain", f"{sym} {bucket.label}: {e}")
                self._chains[key] = None
        return self._chains[key]

    def _error(self, kind: str, msg: str) -> None:
        log.warning("options %s: %s", kind, msg)
        self._tick_errors += 1
        self._errors += 1
        try:
            ostore.event(self.con, self._dstr, self._now, "error", kind, msg)
        except Exception:
            pass
        if self._errors >= self.cfg.max_consecutive_errors and ostore.notify_once(self.con, f"{self._dstr}:errors:{kind}"):
            self._notify("期權軌道異常", f"連續錯誤 {self._errors} 次(最近:{kind} {msg[:300]})。出場會持續重試。",
                         notify.WARN)

    def _notify(self, title: str, msg: str, priority: int) -> None:
        try:
            self._io()
            self.notifier(title, msg, priority)
        except Exception as e:  # a notification failure never breaks the loop
            log.warning("notify failed: %s", e)
