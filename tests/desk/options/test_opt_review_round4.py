"""Round-4 adversarial review of the options engine: S1 (a lagging broker
position read written off as a shortfall), S2 (a minute-bars outage blocking
the premium stop), S3 (the pre-close alert on a slow tick), S4 (flatten_now
under a slow failing broker), S5 (a transient risk-gate error consuming the
once-a-day refusal page). Fake brokers only; no real order is ever sent."""
from datetime import timedelta, timezone

import pytest

from stockmoney.desk import store
from stockmoney.desk.options import _iface
from stockmoney.desk.options import store as ostore
from stockmoney.desk.options._iface import AccountSnapshot, AssetClass, BrokerPosition, Side, occ_symbol
from stockmoney.desk.options.engine import Engine

from opt_fakes import DAY, NEXT, Clock, FakeOptionsBroker, chain, et, orb_breakout_day, spy_cfg, with_history
from test_opt_engine import Rig

C601N = occ_symbol("SPY", NEXT, "C", 601)


@pytest.fixture
def con(tmp_path):
    with store.connect(tmp_path / "desk.sqlite") as c:
        yield c


def titles(r):
    return [a[0] for a in r.sent]


class LagBroker(FakeOptionsBroker):
    """Position reads reflect the state lag_s seconds ago (orders are current)."""

    def __init__(self, *a, lag_s=90.0, **k):
        super().__init__(*a, **k)
        self.lag_s = lag_s
        self.hist = [(None, {})]

    def _try_fill(self, o):
        before = dict(self.positions)
        super()._try_fill(o)
        if self.positions != before:
            self.hist.append((self.clock.now, dict(self.positions)))

    def account(self):
        cut = self.clock.now - timedelta(seconds=self.lag_s)
        pos = {}
        for t, p in self.hist:
            if t is None or t <= cut:
                pos = p
        out = []
        for s, q in pos.items():
            k = self.option_quote(s)
            out.append(BrokerPosition(s, AssetClass.OPTION, q, 0, q * k.mid * 100, k.mid))
        return AccountSnapshot(0, 0, 0, out, self.clock.now)


def lag_rig(tmp_path, con, lag):
    clock = Clock(et(9, 20))
    b = LagBroker(clock, {"SPY": with_history(orb_breakout_day())}, lag_s=lag)
    b.templates = {**chain(), **chain(expiry=NEXT)}
    cfg = spy_cfg(tmp_path, time_stop_minutes=400)
    gate = _iface.make_risk_gate(con, b, cfg)
    sent = []
    eng = Engine(con, b, cfg, clock=clock, risk_gate=gate, live=True, flags_fn=lambda n: {},
                 notifier=lambda *a: sent.append(a))
    return clock, b, eng, sent


def run_eng(eng, clock, until, step=20):
    while clock.now < until.astimezone(timezone.utc):
        eng.tick()
        clock.advance(step)


# ------------------------------------------------------------------ S1 shortfall needs two observations
@pytest.mark.parametrize("lag", [30.0, 90.0])
def test_s1_position_read_lag_after_a_normal_fill_is_never_written_off(tmp_path, con, lag):
    clock, b, eng, sent = lag_rig(tmp_path, con, lag)
    run_eng(eng, clock, et(10, 0))
    assert ostore.positions(con, str(DAY), "live"), "no live entry happened"
    run_eng(eng, clock, et(15, 50))
    wo = con.execute("SELECT * FROM options_fills WHERE reason='broker_shortfall'").fetchall()
    assert not wo, [dict(w) for w in wo]
    assert store.get_setting(con, _iface.OPTIONS_HALT_SETTING, "0") != "1"
    assert not b.positions, f"broker not flat at 15:50: {b.positions}"
    assert not any("券商口數少於資料庫" in s[0] or "未知期權部位" in s[0] for s in sent)


def test_s1_owner_sale_written_off_only_on_the_second_observation_and_hits_the_daily_stop(tmp_path, con):
    r = Rig(tmp_path, con, time_stop_minutes=600, capital_budget=10_000.0)  # daily stop 200
    r.gate = _iface.make_risk_gate(con, r.broker, r.cfg)
    r.eng = r.new_engine()
    r.run(et(9, 52))
    lp = r.pos("live")[0]
    r.broker.positions[C601N] = 4  # the owner sold 6 by hand
    r.clock.set(et(10, 0))
    r.eng.tick()  # first observation: nothing written off yet
    p = ostore.position(con, lp["position_id"])
    assert p["qty_open"] == 10 and not any("券商口數少於資料庫" in t for t in titles(r))
    assert store.get_setting(con, _iface.OPTIONS_HALT_SETTING, "0") != "1"
    r.clock.set(et(10, 1, 30))
    r.eng.tick()  # too soon (< 120 s after the first)
    assert ostore.position(con, lp["position_id"])["qty_open"] == 10
    r.clock.set(et(10, 2, 40))
    r.eng.tick()  # second consistent observation: written off, halted, paged
    p = ostore.position(con, lp["position_id"])
    assert p["qty_open"] == 4 and p["state"] == "ENTERED"
    assert store.get_setting(con, _iface.OPTIONS_HALT_SETTING, "0") == "1"
    assert sum("券商口數少於資料庫" in t for t in titles(r)) == 1
    st = _iface.sleeve_state(con, r.gate.engine.limits, {C601N: 0.95}, r.clock.now)
    assert st.realized_today == pytest.approx(-6 * lp["entry_price"] * 100) and st.daily_pnl <= -200


def test_s1_shortfall_observation_resets_when_the_broker_catches_up(tmp_path, con):
    r = Rig(tmp_path, con, time_stop_minutes=600)
    r.run(et(9, 52))
    lp = r.pos("live")[0]
    r.broker.positions[C601N] = 4
    r.clock.set(et(10, 0))
    r.eng.tick()  # first observation
    r.broker.positions[C601N] = 10  # the read was wrong: the broker holds all 10 again
    r.clock.set(et(10, 1, 10))
    r.eng.tick()  # resets
    r.broker.positions[C601N] = 4
    r.clock.set(et(10, 2, 20))
    r.eng.tick()  # a new first observation, not a second one
    assert ostore.position(con, lp["position_id"])["qty_open"] == 10
    r.clock.set(et(10, 4, 30))
    r.eng.tick()
    assert ostore.position(con, lp["position_id"])["qty_open"] == 4


def test_s1_reappearing_contracts_are_paged_during_the_session_without_a_restart(tmp_path, con):
    r = Rig(tmp_path, con, time_stop_minutes=600)
    r.gate = _iface.make_risk_gate(con, r.broker, r.cfg)
    r.eng = r.new_engine()
    r.run(et(9, 52))
    r.clock.set(et(15, 45))
    r.run(et(15, 46), step=2)
    assert C601N not in r.broker.positions
    r.broker.positions[C601N] = 6  # contracts reappear (owner trade); same engine, no restart
    r.run(et(15, 52), step=20)
    assert any("未知期權部位" in t for t in titles(r))
    assert sum(o.filled_qty for o in r.broker.orders.values() if o.side == Side.SELL) == 10
    assert r.broker.positions[C601N] == 6  # never sold


# ------------------------------------------------------------------ S2 bars outage vs premium stop
@pytest.mark.parametrize("err", ["429 Too Many Requests", "403 subscription does not permit querying recent SIP data"])
def test_s2_bars_outage_never_blocks_the_premium_stop(tmp_path, con, err):
    clock, b, eng, sent = lag_rig(tmp_path, con, 0.0)
    run_eng(eng, clock, et(9, 55))
    lp = ostore.positions(con, str(DAY), "live")[0]
    assert lp["state"] == "ENTERED" and lp["inv_close"] is not None
    b.data_error = RuntimeError(err)  # minute_bars fails from now on
    b.mult[lp["occ"]] = 0.4           # premium -60%: far past the -35% stop
    run_eng(eng, clock, et(10, 5), step=20)
    exits = [o for o in ostore.orders_for(con, lp["position_id"]) if o["kind"] == "exit"]
    assert exits and exits[0]["reason"] == "premium_stop", f"no exit after 10 minutes at -60% while bars fail ({err})"


# ------------------------------------------------------------------ S3 / S4 slow failing broker
class SlowFailBroker(FakeOptionsBroker):
    """From ``slow_from`` every broker call costs ``delay`` seconds of
    (simulated) time and fails."""
    slow_from = None
    delay = 5.0
    calls = 0

    max_attempts = 4  # like AlpacaBroker: a failing call costs delay x max_attempts / 4

    def _slow(self):
        if self.slow_from is not None and self.clock.now >= self.slow_from:
            self.calls += 1
            self.clock.advance(self.delay * self.max_attempts / 4)
            raise ConnectionError("broker unavailable (slow failure)")


for _name in ("minute_bars", "stock_quote", "option_quote", "option_chain", "submit", "get_order", "cancel",
              "open_orders", "account"):
    def _wrap(name):
        base = getattr(FakeOptionsBroker, name)

        def f(self, *a, **k):
            self._slow()
            return base(self, *a, **k)
        return f
    setattr(SlowFailBroker, _name, _wrap(_name))


MORE = ("QQQ", "IWM", "DIA", "XLK", "XLF", "SMH", "XLE", "XLV")


def slow_rig(tmp_path, con, delay, **kw):
    """One live SPY position; eight more symbols in the universe (each scan
    costs broker calls)."""
    r = Rig(tmp_path, con, time_stop_minutes=600, index=("SPY",) + MORE, **kw)
    b = SlowFailBroker(r.clock, {**r.broker.bars, **{s: r.broker.bars["SPY"] for s in MORE}})
    b.templates = {**r.broker.templates, **{k: v for u in MORE[:2] for k, v in {**chain(u), **chain(u, NEXT)}.items()}}
    b.delay = delay
    r.broker = b
    r.eng = r.new_engine()
    r.pages = []
    r.eng.notifier = lambda t, m, p: r.pages.append((r.clock.now, t, p))
    r.eng._sleep = lambda s: r.clock.advance(s)
    r.run(et(9, 52))
    assert r.pos("live")[0]["state"] == "ENTERED"
    r.live_n = len([p for p in ostore.positions(con, str(DAY), "live") if p["state"] == "ENTERED"])
    return r


def session_loop(r, until, poll=20.0):
    """run.session's loop: a full tick, then poll_seconds (2 s while an order works)."""
    while r.clock.now <= until.astimezone(r.clock.now.tzinfo):
        try:
            r.eng.tick()
        except Exception:
            r.con.rollback()
        r.clock.advance(poll)


@pytest.mark.parametrize("delay", [5.0, 20.0])
def test_s3_pre_close_alert_fires_on_time_while_every_call_is_slow_and_failing(tmp_path, con, delay, caplog):
    r = slow_rig(tmp_path, con, delay)
    r.clock.set(et(15, 40))
    r.broker.slow_from = r.clock.now
    session_loop(r, et(15, 56))
    alert = [t for t, title, _ in r.pages if title == "期權軌道收盤前未平倉"]
    assert alert, "no pre-close alert"
    t50 = et(15, 50).astimezone(alert[0].tzinfo)
    # checked at the top of the tick and after each live position, at the time it is then: at
    # most one position's broker calls (about 5) and the alert's own broker read after 15:50
    assert (alert[0] - t50).total_seconds() <= 6 * delay, f"alert at {alert[0]}"
    if delay == 5.0:
        assert alert[0] < et(15, 51).astimezone(alert[0].tzinfo), f"alert at {alert[0]}"
    else:
        assert "options tick running late" in caplog.text  # scans and shadow work were skipped


@pytest.mark.parametrize("delay", [43.0, 127.0])
def test_s4_finish_pages_before_1615_when_every_call_is_slow_and_failing(tmp_path, con, delay):
    r = slow_rig(tmp_path, con, delay)
    r.clock.set(et(15, 44))
    r.broker.slow_from = et(15, 44, 50).astimezone(r.clock.now.tzinfo)
    session_loop(r, et(15, 59, 59))
    loop_end = r.clock.now
    # run._finish
    r.eng.flatten_now("session_end")
    assert not r.eng.finish()
    page = [t for t, title, _ in r.pages if title == "期權軌道收盤未平倉"]
    assert r.live_n == 3
    assert page and page[0] < et(16, 15).astimezone(page[0].tzinfo), (loop_end, page)
    assert r.broker.max_attempts == r.cfg.flat_broker_max_attempts == 2  # fewer retries after the flat


def test_s5_transient_gate_error_does_not_consume_the_once_a_day_refusal_page(tmp_path, con):
    from opt_fakes import FnGate
    from test_opt_review_fixes import entered_rig, exits
    from stockmoney.desk.options._iface import RiskDecision
    gate = FnGate(lambda i, c, n: RiskDecision(True, "ok", i),
                  exit_fn=lambda i, c, n: RiskDecision(False, "risk gate error: BrokerUnavailable: read failed"))
    r, lp = entered_rig(tmp_path, con, gate=gate)
    r.broker.mult[C601N] = 0.6
    r.run(et(9, 54))
    assert not exits(con, lp["position_id"])
    assert not any("出場被風控擋下" in t for t in titles(r))
    assert any("出場風控暫時讀取失敗" in t for t in titles(r))
    assert ostore.notify_once(con, f"{DAY}:exit_refused:{lp['position_id']}")  # key still free
    con.execute("DELETE FROM options_notified WHERE key=?", (f"{DAY}:exit_refused:{lp['position_id']}",))
    # a real refusal later the same day still pages
    gate.exit_fn = lambda i, c, n: RiskDecision(False, "no")
    r.run(et(9, 56))
    assert sum("出場被風控擋下" in t for t in titles(r)) == 1
