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

Every trigger opens a SHADOW position per contract variant (DTE bucket x
delta band), filled at the ask by the shadow model, regardless of live
eligibility. A setup that is live-eligible (config ``live_sources``, the
session is live, the broker is paper unless config says otherwise, the plan
arrived before 09:20 ET, no Grok avoid entry) additionally sends ONE real
order for its live variant, but only if HALT is absent, Muse has no block
flag on the underlying, the engine's own position cap is free and the
injected risk gate allows it. The risk gate may shrink an order, never grow it.

Exits (OPTIONS_PLAN.md section 5, the same for shadow and live, checked in
this order): forced flat at 15:45 ET; underlying invalidation level; time
stop (180 min, or the plan's shorter max_hold_minutes); premium stop -35% on
the quote mid; +40% on the mid takes half (all of a single contract) and
moves the stop on the rest to cost. Price-based exits need a fresh quote;
forced flat, invalidation and the time stop do not.

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
from stockmoney.desk.calendar import is_trading_day
from stockmoney.desk.options import plan as oplan
from stockmoney.desk.options import playbooks as pb
from stockmoney.desk.options import select as sel
from stockmoney.desk.options import shadow as sh
from stockmoney.desk.options import store as ostore
from stockmoney.desk.options import universe as uni
from stockmoney.desk.options._iface import (
    TIF, AssetClass, OptionContract, OrderIntent, OrderStatus, OrderType, RiskGate, Side,
)
from stockmoney.desk.options.config import GROK, RULE_PLAYBOOKS, OptionsConfig

log = logging.getLogger("desk.options")
ET = ZoneInfo("America/New_York")

WAITING, TRIGGERED, ENTERED, EXITED = "WAITING", "TRIGGERED", "ENTERED", "EXITED"
EXPIRED, MISSED, SKIPPED, SUPERSEDED = "EXPIRED", "MISSED", "SKIPPED", "SUPERSEDED"
PENDING, ABANDONED = "PENDING", "ABANDONED"
_TERMINAL_ORDER = {OrderStatus.FILLED.value, OrderStatus.CANCELED.value, OrderStatus.EXPIRED.value,
                   OrderStatus.REJECTED.value, "not_sent"}
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
        self._errors = 0
        self._plan_seen: str | None = None
        ostore.ensure_schema(con)

    # ================================================================ tick
    def tick(self) -> dict:
        now = self.clock()
        et = _et(now)
        day = et.date()
        self._now, self._day, self._dstr = now, day, str(day)
        self._quotes: dict[str, OptionContract | None] = {}
        self._chains: dict[tuple, list[OptionContract]] = {}
        self._spots: dict[str, float | None] = {}
        self._flags: dict[str, str] | None = None
        self._tick_errors = 0
        self._ensure_day(now)
        halted = self.cfg.halt_file.exists()
        if halted and ostore.notify_once(self.con, f"{self._dstr}:halt"):
            self._notify("期權軌道:HALT 生效", "data/desk/HALT 存在:新進場全擋(影子照記),出場照常。", notify.WARN)
        if et >= _at(day, self.cfg.open_time):
            for m in ostore.universe(self.con, self._dstr):
                try:
                    self._scan_symbol(m, now, halted)
                except Exception as e:  # one symbol's bad data must not stop the loop
                    self._error("scan", f"{m['symbol']}: {type(e).__name__}: {e}")
        self._manage_pending(now)
        self._manage_open(now)
        self._roll_setups()
        self._errors = 0 if self._tick_errors == 0 else self._errors
        self.con.commit()
        return self.summary()

    def summary(self) -> dict:
        rows = self.con.execute("SELECT mode, state, COUNT(*) n FROM options_positions WHERE day=? GROUP BY mode, state",
                                (self._dstr,)).fetchall()
        return {f"{r['mode']}_{r['state'].lower()}": r["n"] for r in rows}

    # ================================================================ day setup
    def _ensure_day(self, now: datetime) -> None:
        cfg, d = self.cfg, self._dstr
        active = oplan.active_plan(self.con, self._day)
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
        avoid = {a.ticker for a in active.plan.avoid} if active else set()
        for m in members:
            for src in RULE_PLAYBOOKS:
                self._new_setup(f"{d}:{src}:{m.symbol}", src, m.symbol, m.uclass, now, avoid=avoid)
        ostore.event(self.con, d, now, "info", "universe", f"{len(members)} underlyings: "
                     + ",".join(m.symbol for m in members))

    def _add_plan(self, active, now: datetime) -> None:
        """Create grok_plan setups for the plan in force; WAITING setups of an
        older plan for the same day are superseded (never doubled)."""
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
                ostore.save_universe(self.con, d, [(sym, self.cfg.underlying_class(sym), "plan", self._prev_close(sym),
                                                    self._pm_dollar_vol(sym, now))])
                have.add(sym)
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
        bars = self._refresh_bars(sym, now)
        done = pb.completed(bars, now)
        if et >= _at(self._day, self.cfg.open_time) + timedelta(minutes=2):
            last_close = done.index[-1] + pb.ONE_MIN if not done.empty else None
            if last_close is None or (now - last_close.to_pydatetime()).total_seconds() > self.cfg.max_bar_age_s:
                if ostore.notify_once(self.con, f"{self._dstr}:stale_bars:{sym}"):  # once per symbol per day
                    ostore.event(self.con, self._dstr, now, "warn", "stale_bars", f"{sym}: last bar {last_close}")
                    self._notify("期權軌道:資料延遲", f"{sym} 的 1 分鐘 K 過舊({last_close}),暫停該標的的新進場。",
                                 notify.WARN)
                return
        for s in waiting:
            sig = self._evaluate(s, done, now, m.get("prev_close"), {"pm_dollar_vol": m.get("pm_dollar_vol")})
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

    def _on_signal(self, s: dict, sig: pb.Signal, bars: pd.DataFrame, now: datetime, halted: bool) -> None:
        cfg, d, sid, sym = self.cfg, self._dstr, s["setup_id"], s["underlying"]
        tail = [{"t": ts.isoformat(), **{k: float(v) for k, v in r.items() if k in ("open", "high", "low", "close",
                                                                               "volume", "vwap")}}
                for ts, r in bars.tail(5).iterrows()]
        info = {**sig.to_dict(), "bars": tail, "market": self._market_state(), "seen_at": ostore.iso(now)}
        ostore.update_setup(self.con, sid, state=TRIGGERED, direction=sig.direction,
                            signal_json=json.dumps(info, ensure_ascii=False))
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
        q = self.broker.stock_quote(sym)
        if not q or q[0] <= 0 or q[1] < q[0]:
            return self._skip(s, now, "no_underlying_quote")
        spot = (q[0] + q[1]) / 2
        if (q[1] - q[0]) / spot > cfg.underlying_max_spread_pct:
            return self._skip(s, now, "underlying_spread_wide", {"bid": q[0], "ask": q[1]})
        if (sig.direction == "long" and spot <= sig.invalidation) or (sig.direction == "short" and spot >= sig.invalidation):
            return self._skip(s, now, "already_invalidated", {"spot": spot, "invalidation": sig.invalidation})
        max_hold = cfg.time_stop_minutes
        spec = json.loads(s["spec_json"] or "{}")
        if s["source"] == GROK:
            max_hold = min(max_hold, int(spec["item"]["max_hold_minutes"]))
        flags = self._muse_flags(now)
        ctx = {"muse_flag": flags.get(sym, ""), "halted": halted, "plan_id": s["plan_id"] or "",
               "plan_shadow_only": bool(spec.get("shadow_only", False)), "received_at": spec.get("received_at")}
        live_dte, live_band = self._live_variant(s, spec)
        live_choice = None
        opened = 0
        for bucket in cfg.dte_buckets:
            chain = self._chain(sym, bucket)
            if chain is None:
                ostore.filtered(self.con, d, now, "contract", sym, "chain_error", sid, bucket.label)
                continue
            for band in cfg.delta_bands:
                variant = f"{bucket.label}|{band.label}"
                choice, why, detail = sel.choose(chain, spot, now, sig.direction, bucket, band, cfg)
                if choice is None:
                    ostore.filtered(self.con, d, now, "contract", sym, why, sid, variant, detail)
                    continue
                opened += self._open_shadow(s, sig, choice, now, max_hold, ctx)
                if (bucket.label, band.label) == (live_dte, live_band):
                    live_choice = choice
        pending = self._open_live(s, sig, live_choice, now, halted, flags, max_hold, ctx) if s["live_eligible"] else False
        if opened or pending:
            ostore.update_setup(self.con, sid, state=ENTERED)
        else:
            ostore.update_setup(self.con, sid, state=SKIPPED)

    def _skip(self, s: dict, now: datetime, why: str, detail: dict | None = None) -> None:
        ostore.update_setup(self.con, s["setup_id"], state=SKIPPED)
        ostore.filtered(self.con, self._dstr, now, "entry", s["underlying"], why, s["setup_id"], detail=detail)

    def _live_variant(self, s: dict, spec: dict) -> tuple[str, str]:
        dte = self.cfg.live_dte
        if s["source"] == GROK and spec["item"]["preferred_dte"] != "any":
            dte = spec["item"]["preferred_dte"]
        return dte, self.cfg.live_delta_band

    def _muse_flags(self, now: datetime) -> dict[str, str]:
        if self._flags is None:
            try:
                self._flags = dict(self.flags_fn(now))
            except Exception as e:  # unknown flags: block every live entry this tick (reduce risk, never add)
                self._error("flags", str(e))
                self._flags = {"*": f"flag read failed: {e}"}
        return self._flags

    # ================================================================ entries
    def _open_shadow(self, s: dict, sig: pb.Signal, ch: sel.Choice, now: datetime, max_hold: int, ctx: dict) -> int:
        cfg, c = self.cfg, ch.contract
        price = sh.buy_price(c.ask, cfg)
        qty = sh.contracts_for(price, cfg)
        pid = f"{s['setup_id']}:shadow:{ch.dte_label}:{ch.band_label}"
        if qty < 1:
            ostore.filtered(self.con, self._dstr, now, "contract", s["underlying"], "premium_above_cap", s["setup_id"],
                            f"{ch.dte_label}|{ch.band_label}", {"ask": c.ask})
            return 0
        row = self._position_row(s, sig, ch, pid, "shadow", now, max_hold, ctx)
        row.update(qty_initial=qty, qty_open=qty, state=ENTERED, entry_time=ostore.iso(now), entry_price=price,
                   fees_usd=cfg.fee_per_contract * qty, spread_cost_usd=sh.half_spread_cost(c.bid, c.ask, qty, cfg))
        if ostore.insert_position(self.con, row):
            ostore.add_fill(self.con, pid, now, "buy", qty, price, c.bid, c.ask, "entry")
        return 1

    def _position_row(self, s, sig, ch: sel.Choice, pid, mode, now, max_hold, ctx) -> dict:
        c = ch.contract
        return {"position_id": pid, "setup_id": s["setup_id"], "day": self._dstr, "mode": mode, "source": s["source"],
                "underlying": s["underlying"], "uclass": s["uclass"], "dte_label": ch.dte_label,
                "band_label": ch.band_label, "occ": c.occ, "right": c.right, "strike": c.strike,
                "expiry": str(c.expiry), "entry_bid": c.bid, "entry_ask": c.ask,
                "entry_quote_json": json.dumps(ch.quote_dict()), "invalidation": sig.invalidation,
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
        n_live = self.con.execute("SELECT COUNT(*) FROM options_positions WHERE day=? AND mode='live' AND state IN (?,?)",
                                  (d, PENDING, ENTERED)).fetchone()[0]
        if n_live >= cfg.max_open_positions:
            return block("max_open_positions")
        n_sym = self.con.execute("SELECT COUNT(*) FROM options_positions WHERE day=? AND mode='live' AND underlying=? "
                                 "AND state IN (?,?)", (d, sym, PENDING, ENTERED)).fetchone()[0]
        if n_sym >= cfg.max_live_per_underlying:
            return block("max_live_per_underlying")
        c = ch.contract
        limit = sh.round_tick(c.ask + cfg.entry_limit_offset, sh.option_tick(sym, c.ask), up=True)
        qty = sh.contracts_for(limit, cfg)
        if qty < 1:
            return block("premium_above_cap", {"limit": limit})
        pid = f"{sid}:live:{ch.dte_label}:{ch.band_label}"
        intent = OrderIntent(ostore.cid(pid, "e"), c.occ, AssetClass.OPTION, Side.BUY, qty=qty,
                             order_type=OrderType.LIMIT, tif=TIF.DAY, limit_price=limit,
                             strategy=f"options:{s['source']}", reason=sig.reason[:200])
        try:
            dec = self.risk_gate(intent, c, qty * limit * cfg.contract_multiplier, now)
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
        row = self._position_row(s, sig, ch, pid, "live", now, max_hold, ctx)
        row.update(qty_initial=final.qty, qty_open=0, state=PENDING)
        if not ostore.insert_position(self.con, row):
            return True  # already exists (re-run): the pending order is resolved by _manage_pending
        self._submit(pid, final, "entry", "entry", now)
        return True

    def _submit(self, pid: str, intent: OrderIntent, kind: str, reason: str, now: datetime) -> None:
        """Commit the order row first, then submit. A second call for the same
        client_order_id never reaches the broker again."""
        if not ostore.record_order(self.con, intent.client_order_id, pid, kind, intent.side.value, intent.qty,
                                   intent.order_type.value, intent.limit_price, reason, now):
            return
        try:
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
        for p in ostore.positions(self.con, self._dstr, "live", (PENDING,)):
            try:
                self._resolve_entry(p, now)
            except Exception as e:
                self._error("entry_order", f"{p['position_id']}: {type(e).__name__}: {e}")

    def _broker_order(self, row: dict, now: datetime):
        """(order or None, gone). ``gone`` = the broker never received it."""
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
            return
        o, gone = self._broker_order(row, now)
        if gone:
            ostore.update_position(self.con, pid, state=ABANDONED, exit_reason="entry_not_sent", exit_time=ostore.iso(now))
            return
        if o is None:
            return
        delta = (o.filled_qty or 0) - (row["booked_qty"] or 0)
        if delta > 0 and o.filled_avg_price:
            ostore.add_fill(self.con, pid, now, "buy", delta, o.filled_avg_price, p["entry_bid"], p["entry_ask"],
                            "entry", f"{ecid}#{o.filled_qty:g}")
            ostore.update_position(self.con, pid, qty_open=(p["qty_open"] or 0) + delta, entry_price=o.filled_avg_price,
                                   fees_usd=cfg.fee_per_contract * o.filled_qty,
                                   spread_cost_usd=sh.half_spread_cost(p["entry_bid"], p["entry_ask"], o.filled_qty, cfg))
            ostore.update_order(self.con, ecid, booked_qty=o.filled_qty, filled_qty=o.filled_qty,
                                avg_price=o.filled_avg_price)
        status = o.status.value
        ostore.update_order(self.con, ecid, status=status)
        if status in _TERMINAL_ORDER:
            if o.filled_qty and o.filled_qty > 0:
                ostore.update_position(self.con, pid, state=ENTERED, qty_initial=o.filled_qty,
                                       entry_time=ostore.iso(o.filled_at or now))
                self._notify("期權紙上進場", f"{p['underlying']} {p['occ']} x{o.filled_qty:g} @ {o.filled_avg_price:.2f}"
                             f"({p['source']})", notify.INFO)
            else:
                ostore.update_position(self.con, pid, state=ABANDONED, exit_reason="entry_unfilled",
                                       exit_time=ostore.iso(now))
            return
        age = (now - datetime.fromisoformat(row["submitted_at"])).total_seconds()
        if age >= cfg.entry_timeout_s or _et(now) >= _at(self._day, cfg.force_flat):
            self.broker.cancel(ecid)
            ostore.update_order(self.con, ecid, note="cancel requested (entry timeout)")

    # ================================================================ exits
    def _manage_open(self, now: datetime) -> None:
        for p in ostore.positions(self.con, self._dstr, None, (ENTERED,)):
            try:
                self._manage_one(p, now)
            except Exception as e:
                self._error("exit", f"{p['position_id']}: {type(e).__name__}: {e}")

    def _quote(self, occ: str) -> OptionContract | None:
        if occ not in self._quotes:
            try:
                self._quotes[occ] = self.broker.option_quote(occ)
            except Exception as e:
                self._error("quote", f"{occ}: {e}")
                self._quotes[occ] = None
        return self._quotes[occ]

    def _fresh(self, q: OptionContract | None, now: datetime) -> bool:
        return (q is not None and sel.mid(q) is not None and q.as_of is not None
                and (now - q.as_of).total_seconds() <= self.cfg.max_quote_age_s)

    def _manage_one(self, p: dict, now: datetime) -> None:
        q = self._quote(p["occ"])
        fresh = self._fresh(q, now)
        if fresh:
            ostore.update_position(self.con, p["position_id"], last_bid=q.bid, last_ask=q.ask,
                                   max_bid=max(p["max_bid"] or q.bid, q.bid), min_bid=min(p["min_bid"] or q.bid, q.bid))
            p = ostore.position(self.con, p["position_id"])
        if p["mode"] == "live":
            working = [o for o in ostore.orders_for(self.con, p["position_id"])
                       if o["kind"] == "exit" and o["status"] not in _TERMINAL_ORDER]
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
        else:
            self._send_exit(p, qty, reason, q if fresh else None, now)

    def _exit_decision(self, p: dict, q: OptionContract | None, now: datetime) -> tuple[str, float]:
        cfg = self.cfg
        qty = p["qty_open"]
        if _et(now) >= _at(self._day, cfg.force_flat):
            return "force_flat", qty
        spot = self._spot(p["underlying"], now)
        inv = p["invalidation"]
        if spot is not None and inv is not None:
            if (p["direction"] == "long" and spot <= inv) or (p["direction"] == "short" and spot >= inv):
                return "invalidation", qty
        held = (now - datetime.fromisoformat(p["entry_time"])).total_seconds() / 60
        if held >= (p["max_hold_min"] or cfg.time_stop_minutes):
            return "time_stop", qty
        if q is None:
            return "", 0
        m, e = sel.mid(q), p["entry_price"]
        if not p["tp_done"]:
            if m <= e * (1 - cfg.premium_stop_pct):
                return "premium_stop", qty
            if m >= e * (1 + cfg.take_profit_pct):
                part = floor(qty * cfg.take_profit_fraction) if qty >= 2 else qty
                return "take_profit", part
        elif m <= (p["stop_price"] or e):
            return "breakeven_stop", qty
        return "", 0

    def _spot(self, sym: str, now: datetime) -> float | None:
        if sym not in self._spots:
            v = None
            try:
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
            fields.update(tp_done=1, stop_price=p["entry_price"])
        if left <= 1e-9:
            fields.update(state=EXITED, exit_time=ostore.iso(now), exit_reason=reason)
        if note:
            fields["note"] = ((p["note"] or "") + "; " + note).strip("; ")[:500]
        ostore.update_position(self.con, pid, **fields)

    def _send_exit(self, p: dict, qty: float, reason: str, q: OptionContract | None, now: datetime) -> None:
        cfg, pid = self.cfg, p["position_id"]
        exits = [o for o in ostore.orders_for(self.con, pid) if o["kind"] == "exit"]
        streak = 0
        for o in reversed(exits):
            if (o["filled_qty"] or 0) > 0:
                break
            streak += 1
        if q is not None and q.bid and streak < cfg.exit_max_limit_attempts:
            otype, limit = OrderType.LIMIT, sh.round_tick(q.bid, sh.option_tick(p["underlying"], q.bid), up=False)
        else:
            otype, limit = OrderType.MARKET, None
        intent = OrderIntent(ostore.cid(pid, f"x{len(exits)}"), p["occ"], AssetClass.OPTION, Side.SELL, qty=qty,
                             order_type=otype, tif=TIF.DAY, limit_price=limit, strategy=f"options:{p['source']}",
                             reason=reason)
        self._submit(pid, intent, "exit", reason, now)

    def _resolve_exit(self, p: dict, row: dict, now: datetime) -> None:
        cid = row["client_order_id"]
        o, gone = self._broker_order(row, now)
        if o is None:
            return
        delta = (o.filled_qty or 0) - (row["booked_qty"] or 0)
        if delta > 0 and o.filled_avg_price is not None:
            self._book_exit(p, delta, o.filled_avg_price, p["last_bid"], p["last_ask"], row["reason"], now,
                            client_order_id=f"{cid}#{o.filled_qty:g}")
            ostore.update_order(self.con, cid, booked_qty=o.filled_qty, filled_qty=o.filled_qty,
                                avg_price=o.filled_avg_price)
            p = ostore.position(self.con, p["position_id"])
            if p["state"] == EXITED:
                self._notify("期權紙上出場", f"{p['underlying']} {p['occ']}:{p['exit_reason']},"
                             f"損益 {p['realized_usd'] - p['fees_usd']:+.0f} 美元", notify.INFO)
        ostore.update_order(self.con, cid, status=o.status.value)
        if o.status.value in _TERMINAL_ORDER:
            return
        age = (now - datetime.fromisoformat(row["submitted_at"])).total_seconds()
        if age >= self.cfg.exit_timeout_s:
            self.broker.cancel(cid)
            ostore.update_order(self.con, cid, note="cancel requested (reprice)")

    # ================================================================ wrap-up
    def _roll_setups(self) -> None:
        for s in ostore.setups(self.con, self._dstr):
            if s["state"] != ENTERED:
                continue
            open_ = self.con.execute("SELECT COUNT(*) FROM options_positions WHERE setup_id=? AND state IN (?,?)",
                                     (s["setup_id"], PENDING, ENTERED)).fetchone()[0]
            if open_ == 0:
                ostore.update_setup(self.con, s["setup_id"], state=EXITED)

    def finish(self) -> bool:
        """End of session: close any shadow position still open at its last
        known bid, then verify the live book is flat (store AND broker).
        Returns True when flat; pages the owner otherwise."""
        now = self.clock()
        self._now, self._day, self._dstr = now, _et(now).date(), str(_et(now).date())
        for p in ostore.positions(self.con, self._dstr, "shadow", (ENTERED,)):
            self._book_exit(p, p["qty_open"], sh.sell_price(p["last_bid"] or 0.0, self.cfg), p["last_bid"],
                            p["last_ask"], "session_end", now, "closed at last known bid")
        self._roll_setups()
        problems = [f"{p['occ']} ({p['state']})" for p in ostore.positions(self.con, self._dstr, "live", (PENDING, ENTERED))]
        if hasattr(self.broker, "account"):
            try:
                snap = self.broker.account()
                problems += [f"broker {bp.symbol} x{bp.qty:g}" for bp in snap.positions
                             if getattr(bp.asset_class, "value", bp.asset_class) == AssetClass.OPTION.value]
            except Exception as e:
                problems.append(f"cannot read broker positions: {e}")
        self.con.commit()
        if problems:
            ostore.event(self.con, self._dstr, now, "critical", "not_flat", "; ".join(problems))
            self.con.commit()
            self._notify("期權軌道收盤未平倉", "收盤檢查發現期權部位未平:" + "; ".join(problems)[:1500], notify.CRITICAL)
            return False
        return True

    def flatten_now(self, reason: str = "force_flat") -> None:
        """Emergency path: treat every open position as due for the forced exit."""
        now = self.clock()
        for p in ostore.positions(self.con, self._dstr, None, (ENTERED,)):
            q = self._quote(p["occ"])
            fresh = self._fresh(q, now)
            if p["mode"] == "shadow":
                bid = q.bid if fresh else p["last_bid"]
                if bid is not None:
                    self._book_exit(p, p["qty_open"], sh.sell_price(bid, self.cfg), bid,
                                    q.ask if fresh else p["last_ask"], reason, now)
            else:
                self._send_exit(p, p["qty_open"], reason, q if fresh else None, now)
        self.con.commit()

    # ================================================================ plumbing
    def _refresh_bars(self, sym: str, now: datetime) -> pd.DataFrame:
        have = self._bars.get(sym)
        day_start = _at(self._day, (4, 0))
        if have is None or have.empty or have.index[-1].date() != self._day:
            start, have = day_start, None
        else:
            start = (have.index[-1] - pd.Timedelta(minutes=3)).to_pydatetime()
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
            self.notifier(title, msg, priority)
        except Exception as e:  # a notification failure never breaks the loop
            log.warning("notify failed: %s", e)
