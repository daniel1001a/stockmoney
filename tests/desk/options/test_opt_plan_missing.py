"""Missing OPTIONS_PLAN push: once per day from 09:10 ET, only without an accepted plan."""
import json

import pytest

from stockmoney.desk import store
from stockmoney.desk.options import plan as oplan
from stockmoney.desk.options import store as ostore

from opt_fakes import DAY, et
from test_opt_engine import Rig, _plan


@pytest.fixture
def con(tmp_path):
    with store.connect(tmp_path / "desk.sqlite") as c:
        yield c


def missing(r):
    return [a for a in r.sent if "OPTIONS_PLAN" in a[0]]


def events(con):
    return [e for e in con.execute("SELECT * FROM options_events").fetchall() if e["kind"] == "plan_missing"]


def test_not_before_0910(tmp_path, con):
    r = Rig(tmp_path, con)
    r.clock.set(et(9, 9))
    r.eng.tick()
    assert missing(r) == [] and events(con) == []


def test_fires_once_at_0910(tmp_path, con):
    r = Rig(tmp_path, con)
    r.clock.set(et(9, 10))
    r.eng.tick()
    r.clock.set(et(9, 11))
    r.eng.tick()
    assert len(missing(r)) == 1 and missing(r)[0][2] == 4 and len(events(con)) == 1


@pytest.mark.parametrize("text", [None, "empty"])
def test_not_fired_with_accepted_plan(tmp_path, con, text):
    r = Rig(tmp_path, con)
    if text is None:
        raw = _plan()
    else:
        raw = json.dumps({**json.loads(_plan()), "plans": []})
    oplan.ingest(con, raw, et(8, 0))
    assert oplan.active_plan(con, DAY) is not None
    r.clock.set(et(9, 15))
    r.eng.tick()
    assert missing(r) == []


def test_not_twice_across_restart_and_late_plan_silent(tmp_path, con):
    r = Rig(tmp_path, con)
    r.clock.set(et(9, 12))
    r.eng.tick()
    r2 = Rig(tmp_path, con)  # restart: fresh engine, same db
    r2.clock.set(et(9, 14))
    r2.eng.tick()
    assert len(missing(r)) == 1 and missing(r2) == []
    oplan.ingest(con, _plan(), et(9, 30))
    r2.clock.set(et(9, 31))
    r2.eng.tick()
    assert missing(r2) == [] and len(events(con)) == 1
