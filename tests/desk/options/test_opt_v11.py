"""OPTIONS_PLAN schema v1.1 (owner, 2026-10-12): optional trigger /
invalidation, directional priors, key_events window, grok_stance on rule
setups and its report breakdown."""
import json

import pytest

from stockmoney.desk import store
from stockmoney.desk.options import plan as oplan
from stockmoney.desk.options import report as oreport
from stockmoney.desk.options import store as ostore

from opt_fakes import DAY, et
from test_opt_engine import Rig


@pytest.fixture
def con(tmp_path):
    with store.connect(tmp_path / "desk.sqlite") as c:
        yield c


def prior(sym="SPY", bias="long", **extra):
    return {"underlying": sym, "bias": bias, "playbook": "catalyst", "thesis": "t", "catalyst": "news",
            "confidence": 0.5, "evidence_urls": [], **extra}


def plan(items, plan_id="gp-20261013-0800", events=(), avoid=()):
    return json.dumps({
        "protocol": "1", "type": "OPTIONS_PLAN", "plan_id": plan_id, "for_date": str(DAY),
        "generated_at": "2026-10-13T07:45:00-04:00",
        "market_context": {"summary": "", "risk_tone": "mixed",
                           "key_events": [{"time": t, "what": "event"} for t in events]},
        "plans": list(items), "avoid": list(avoid), "notes": ""})


TRIG = {"kind": "break_above", "level": 601.5, "confirm_closes": 1, "earliest": "09:35", "latest": "11:00"}
INV = {"kind": "below", "underlying_level": 599.0}


# ------------------------------------------------------------------ V1 schema
def test_v1_directional_item_minimal_fields():
    p = oplan.parse(plan([prior()]))
    it = p.plans[0]
    assert it.directional and it.trigger is None and it.invalidation is None
    assert it.preferred_dte == "any" and it.max_hold_minutes is None and it.target_underlying_pct is None
    assert oplan.parse(plan([])).plans == []


@pytest.mark.parametrize("why,items", [
    ("trigger without invalidation", [prior(trigger=TRIG)]),
    ("wrong invalidation side", [prior(invalidation={"kind": "above", "underlying_level": 650.0})]),
    ("invalidation on the winning side", [prior(trigger=TRIG, invalidation={"kind": "below", "underlying_level": 602})]),
    ("unknown field", [prior(size=10)]),
    ("missing required confidence", [{k: v for k, v in prior().items() if k != "confidence"}]),
    ("missing required evidence_urls", [{k: v for k, v in prior().items() if k != "evidence_urls"}]),
    ("trigger outside the window", [prior(trigger={**TRIG, "earliest": "09:30"}, invalidation=INV)]),
    ("max_hold out of range", [prior(max_hold_minutes=181)]),
])
def test_v1_rejections(why, items):
    with pytest.raises(ValueError):
        oplan.parse(plan(items))


def test_v1_triggered_item_unchanged():
    it = oplan.parse(plan([prior(trigger=TRIG, invalidation=INV, max_hold_minutes=45)])).plans[0]
    assert not it.directional and it.trigger.level == 601.5


@pytest.mark.parametrize("t,ok", [("08:30", False), ("08:59", False), ("09:00", True), ("15:30", True),
                                  ("15:31", False), ("٠٩:30", False), ("9:30", False)])
def test_v1_key_event_window(t, ok):
    if ok:
        assert oplan.parse(plan([], events=(t,))).market_context.key_events[0].time == t
    else:
        with pytest.raises(ValueError):
            oplan.parse(plan([], events=(t,)))


# ------------------------------------------------------------------ V2 engine
def _stance(con, src="orb15", sym="SPY"):
    r = con.execute("SELECT grok_stance, grok_stance_late, state FROM options_setups WHERE setup_id=?",
                    (f"{DAY}:{src}:{sym}",)).fetchone()
    return tuple(r) if r else None


@pytest.mark.parametrize("bias,want", [("long", "agree"), ("short", "disagree")])
def test_v2_rule_setup_tagged_with_stance_live_unchanged(tmp_path, con, bias, want):
    oplan.ingest(con, plan([prior(bias=bias)]), et(8, 0))
    r = Rig(tmp_path, con)
    r.run(et(9, 52))
    assert _stance(con)[:2] == (want, 0)
    assert not r.pos(source="grok_plan")                       # a prior creates no setup of its own
    assert r.pos("live") and r.pos("live")[0]["source"] == "orb15"  # live eligibility decided by the rules


def test_v2_no_plan_is_none_and_contradicting_priors_are_none(tmp_path, con):
    r = Rig(tmp_path, con)
    r.run(et(9, 52))
    assert _stance(con)[0] == "none"
    with store.connect(tmp_path / "b.sqlite") as con2:
        oplan.ingest(con2, plan([prior(bias="long"), prior(bias="short")]), et(8, 0))
        r2 = Rig(tmp_path, con2)
        r2.run(et(9, 52))
        assert _stance(con2)[0] == "none"


def test_v2_late_plan_still_tagged_and_marked_late(tmp_path, con):
    r = Rig(tmp_path, con)
    r.run(et(9, 24))
    oplan.ingest(con, plan([prior(bias="long")]), et(9, 24))  # after the 09:20 cutoff
    r.run(et(9, 52))
    assert _stance(con)[:2] == ("agree", 1)


def test_v2_prior_adds_its_underlying_to_the_universe(tmp_path, con):
    # before the universe is built: a plan pick
    oplan.ingest(con, plan([prior(sym="QQQ")]), et(8, 0))
    r = Rig(tmp_path, con)
    r.run(et(9, 31))
    assert "QQQ" in {m["symbol"] for m in ostore.universe(con, str(DAY))}
    assert _stance(con, "gap_go", "QQQ") is not None
    # after it is built (late plan): added with its rule setups
    with store.connect(tmp_path / "b.sqlite") as con2:
        r2 = Rig(tmp_path, con2)
        r2.run(et(9, 40))
        oplan.ingest(con2, plan([prior(sym="AMD")], plan_id="gp-20261013-0940"), et(9, 40))
        r2.run(et(9, 41))
        u = {m["symbol"]: m["origin"] for m in ostore.universe(con2, str(DAY))}
        assert u.get("AMD") == "plan_prior"
        assert all(_stance(con2, s, "AMD") is not None for s in ("gap_go", "orb15", "vwap_reclaim", "gap_fade"))


# ------------------------------------------------------------------ V3 report
def test_v3_report_breaks_down_by_stance_with_sample_warning(tmp_path, con):
    oplan.ingest(con, plan([prior(bias="long")]), et(8, 0))
    r = Rig(tmp_path, con)
    r.run(et(9, 52))
    r.clock.set(et(15, 45))
    r.run(et(15, 47))
    r.eng.finish()
    rep = oreport.build(con, str(DAY), r.cfg)
    sec = rep[rep.index("## Grok 方向判斷"):]
    assert "### 紙上實單" in sec and "### 影子主變體" in sec and "影子變體 0dte" in sec
    assert "Grok 同意" in sec and "樣本數警告" in sec and "少於 30 筆" in sec


# ------------------------------------------------------------------ round-3 F5 / F6
PRIOR_NAMES = ("QQQ", "AAPL", "MSFT", "NVDA", "AMD", "TSLA", "META")  # none in spy_cfg's starting list


def test_f5_prior_only_names_capped_at_five(tmp_path, con):
    oplan.ingest(con, plan([prior(sym=s) for s in PRIOR_NAMES]), et(8, 0))
    r = Rig(tmp_path, con)
    r.run(et(9, 26))
    u = {m["symbol"]: m["origin"] for m in ostore.universe(con, str(DAY))}
    assert sorted(s for s, o in u.items() if o == "plan_prior") == sorted(PRIOR_NAMES[:5])
    capped = {x[0] for x in con.execute("SELECT underlying FROM options_filtered WHERE reason='prior_cap'")}
    assert capped == set(PRIOR_NAMES[5:])
    # a later plan cannot add more prior-only names
    oplan.ingest(con, plan([prior(sym="NFLX")], plan_id="gp-20261013-0940"), et(9, 40))
    r.run(et(9, 41))
    assert "NFLX" not in {m["symbol"] for m in ostore.universe(con, str(DAY))}
    # a triggered item is not a prior: not capped
    oplan.ingest(con, plan([prior(sym="NFLX", trigger=TRIG, invalidation=INV)], plan_id="gp-20261013-0942"), et(9, 42))
    r.run(et(9, 43))
    assert "NFLX" in {m["symbol"] for m in ostore.universe(con, str(DAY))}


def test_f5_late_plan_extension_runs_after_exit_management(tmp_path, con):
    r = Rig(tmp_path, con)
    r.run(et(9, 52))
    lp = r.pos("live")[0]
    calls = []
    for name in ("submit", "minute_bars"):
        f = getattr(r.broker, name)
        setattr(r.broker, name, (lambda f, name: lambda *a, **k: (calls.append(
            (name, getattr(a[0], "symbol", a[0]))), f(*a, **k))[1])(f, name))
    r.broker.mult[ostore.position(con, lp["position_id"])["occ"]] = 0.5  # premium stop due now
    oplan.ingest(con, plan([prior(sym="AMD")], plan_id="gp-20261013-0952"), et(9, 52))
    r.eng.tick()
    exit_at = next(i for i, c in enumerate(calls) if c[0] == "submit")
    amd_at = next(i for i, c in enumerate(calls) if c == ("minute_bars", "AMD"))
    assert exit_at < amd_at


def test_f6_plan_retried_when_its_tick_fails(tmp_path, con):
    r = Rig(tmp_path, con)
    r.run(et(9, 40))
    oplan.ingest(con, plan([prior(trigger=TRIG, invalidation=INV)], plan_id="gp-20261013-0940"), et(9, 40))
    real = r.eng._roll_setups  # a failure LATER in the same tick, after the plan was added
    boom = {"n": 1}

    def flaky(*a, **k):
        if boom["n"]:
            boom["n"] -= 1
            raise RuntimeError("disk hiccup")
        return real(*a, **k)
    r.eng._roll_setups = flaky
    r.eng._io = lambda: None  # no broker call (and so no commit) between the plan and the failure
    with pytest.raises(RuntimeError):
        r.eng.tick()
    con.rollback()  # what the session loop does after a failed tick
    r.clock.advance(20)
    r.eng.tick()
    assert con.execute("SELECT COUNT(*) FROM options_setups WHERE source='grok_plan' AND plan_id='gp-20261013-0940'"
                       ).fetchone()[0] == 1
