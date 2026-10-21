"""GROK_DAILY operations: lateness on trading days only, collected-date match,
Grok's own on/off switch, off-list scope notice, FEEDBACK routing. Offline."""
import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from grok_helpers import TOKEN, SENDER, deliver, grok_daily, worst_case

from stockmoney.desk import muse_ops, notify, store
from stockmoney.desk.bridge.inbox import poll
from stockmoney.desk.bridge.mailbox import BridgeConfig, FakeMailbox
from stockmoney.desk.calendar import is_trading_day

CONFIG = BridgeConfig(desk_address="desk@example.com", app_password="x", muse_address=SENDER, shared_token=TOKEN)
ET = muse_ops.ET
MON = datetime(2026, 10, 12, tzinfo=ET)        # Columbus Day: NYSE is open
SAT = datetime(2026, 10, 17, tzinfo=ET)
THANKSGIVING = datetime(2026, 11, 26, tzinfo=ET)
MISS_NOTE = ";沒有收到信(不是空白日報),不代表今天沒有風險"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("DESK_MAIL_ADDRESS", "desk@example.com")
    monkeypatch.setenv("DESK_MAIL_APP_PASSWORD", "x")
    monkeypatch.setenv("MUSE_ADDRESS", SENDER)
    monkeypatch.setenv("MUSE_SHARED_TOKEN", TOKEN)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(store.SCHEMA)
    muse_ops.ensure_schema(c)
    yield c
    c.close()


@pytest.fixture
def pushed(monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "send", lambda title, msg, prio=notify.INFO, tags=None: calls.append((title, msg)) or True)
    return calls


def grok_on(con):
    store.set_setting(con, muse_ops.GROK_EXPECT_KEY, json.dumps(["GROK_AM"]))


def at(day: datetime, h: int, m: int = 0) -> datetime:
    return day.replace(hour=h, minute=m)


def message_for(day: datetime, msg_id: str | None = None, hour: int = 7) -> dict:
    off = day.replace(hour=hour).strftime("%z")
    off = off[:3] + ":" + off[3:]
    d = day.strftime("%Y-%m-%d")
    return grok_daily(msg_id or f"grok-{day:%Y%m%d}-{hour:02d}30", f"{d}T{hour:02d}:30:00{off}",
                      f"{d}T{hour:02d}:05:00{off}")


def test_calendar_assumptions():
    assert is_trading_day(MON.date()) and not is_trading_day(SAT.date()) and not is_trading_day(THANKSGIVING.date())


def test_grok_miss_reported_once_on_a_trading_day_only_when_enabled(con):
    assert muse_ops.check_deliveries(con, at(MON, 9)) == []  # not enabled
    grok_on(con)
    assert muse_ops.check_deliveries(con, at(MON, 8, 15)) == []  # before the deadline
    misses = muse_ops.check_deliveries(con, at(MON, 8, 30))
    assert misses == ["Grok 今天沒有準時送出「Grok 盤前日報」(期限 08:30 ET)" + MISS_NOTE]
    assert muse_ops.check_deliveries(con, at(MON, 9)) == []  # once per day


@pytest.mark.parametrize("day", [SAT, THANKSGIVING])
def test_no_miss_on_weekends_and_holidays_but_message_is_stored(con, day):
    grok_on(con)
    assert muse_ops.check_deliveries(con, at(day, 12)) == []
    box = FakeMailbox()
    deliver(box, message_for(day))
    assert poll(con, box, CONFIG, now=at(day, 7, 45)) == 1


def test_on_time_delivery_counts_and_scorecard_skips_non_trading_days(con):
    grok_on(con)
    box = FakeMailbox()
    deliver(box, message_for(MON))
    poll(con, box, CONFIG, now=at(MON, 7, 45))
    assert muse_ops.check_deliveries(con, at(MON, 9)) == []
    card = muse_ops.scorecard(con, datetime(2026, 10, 18, 12, tzinfo=ET), days=7)  # Mon 12 .. Sun 18
    assert card["deliveries"]["GROK_AM"] == {"name": "Grok 盤前日報", "on_time": 1, "due": 5}


def test_stale_resend_does_not_count_for_today(con):
    grok_on(con)
    box = FakeMailbox()
    deliver(box, message_for(MON - timedelta(days=3), "grok-20261009-0730-r"))  # Friday's digest, re-sent Monday
    poll(con, box, CONFIG, now=at(MON, 7, 45))
    assert len(muse_ops.check_deliveries(con, at(MON, 8, 30))) == 1


def test_received_after_deadline_is_late_in_scorecard(con):
    grok_on(con)
    box = FakeMailbox()
    deliver(box, message_for(MON))
    poll(con, box, CONFIG, now=at(MON, 8, 40))
    card = muse_ops.scorecard(con, at(MON, 12), days=1)
    assert card["deliveries"]["GROK_AM"]["on_time"] == 0


def test_muse_stage_does_not_switch_grok_off_and_union_is_used(con):
    grok_on(con)
    store.set_setting(con, "muse_expect", json.dumps(["SOCIAL_AM", "SOCIAL_PM", "CALLS"]))  # what "stage 2" writes
    assert muse_ops.enabled_expectations(con) == ["SOCIAL_AM", "SOCIAL_PM", "CALLS", "GROK_AM"]


def test_muse_check_grok_on_off(tmp_path, monkeypatch, capsys):
    path = Path(__file__).resolve().parents[2] / "scripts/muse_check.py"
    spec = importlib.util.spec_from_file_location("muse_check_under_test", path)
    mc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mc)
    real = store.connect
    monkeypatch.setattr(store, "connect", lambda p=None: real(tmp_path / "d.sqlite"))
    assert mc.main(["muse_check.py", "grok", "on"]) == 0
    assert mc.main(["muse_check.py", "stage", "2"]) == 0
    with real(tmp_path / "d.sqlite") as c:
        assert "GROK_AM" in muse_ops.enabled_expectations(c) and "SOCIAL_AM" in muse_ops.enabled_expectations(c)
    assert mc.main(["muse_check.py", "grok", "off"]) == 0
    with real(tmp_path / "d.sqlite") as c:
        assert "GROK_AM" not in muse_ops.enabled_expectations(c)
    assert mc.main(["muse_check.py", "grok", "maybe"]) == 2


def test_more_than_five_off_list_tickers_notifies_once_never_rejects(con, pushed):
    d = message_for(MON)
    d["attention"] = [{"ticker": t, "trend": "rising", "why": "x"} for t in ("AAAA", "BBBB", "CCCC", "DDDD", "EEEE")]
    box = FakeMailbox()
    deliver(box, d)
    d2 = message_for(MON, "grok-20261012-0745")
    d2["attention"] = d["attention"] + [{"ticker": "FFFF", "trend": "rising", "why": "x"}]
    deliver(box, d2)
    assert poll(con, box, CONFIG, now=at(MON, 7, 50)) == 2  # both stored
    out = muse_ops.run_checks(con, at(MON, 7, 55))
    assert "grok-20261012-0745" in out and "grok-20261012-0730" not in out
    assert [t for t, _ in pushed] == ["Grok 範圍"]
    muse_ops.run_checks(con, at(MON, 8, 10))
    assert len(pushed) == 1


def test_scope_includes_universe_and_grok_extras():
    scope = muse_ops.grok_scope()
    assert {"SPY", "NVDA", "BRK-B", "QQQ", "TQQQ", "BTC/USD"} <= scope
    assert muse_ops.grok_off_list(message_for(MON)) == []
    assert muse_ops.grok_off_list(worst_case()) == []  # BTC/USD only


def test_bad_grok_message_gets_tagged_feedback_to_the_shared_address(con):
    box = FakeMailbox()
    d = message_for(MON)
    del d["calls"]
    deliver(box, d)
    poll(con, box, CONFIG, now=at(MON, 7, 45))
    assert muse_ops.send_feedback(con, box, at(MON, 7, 50)) == 1
    to, subject, body = box.sent[0]
    fb = json.loads(body)
    assert to == SENDER and subject.startswith("[SMD/FEEDBACK/")
    assert fb["about_msg_id"] == "grok-20261012-0730" and fb["problem"].startswith("【給 Grok】")
    head = fb["how_to_fix"].split("選填")[0]
    for name in ("collected_at", "events", "chains", "calls", "tracked_accounts"):
        assert name in head
    assert "BTC/USD" in fb["how_to_fix"] and len(fb["how_to_fix"]) < 1000


def test_muse_feedback_is_not_tagged(con):
    box = FakeMailbox()
    bad = {"msg_id": "sd-bad-0001", "sent_at": "2026-10-12T07:30:00-04:00", "type": "SOCIAL_DAILY",
           "collected_at": "2026-10-12T07:20:00-04:00", "items": [{"ticker": "NVDA"}]}
    deliver(box, bad)
    poll(con, box, CONFIG, now=at(MON, 7, 45))
    muse_ops.send_feedback(con, box, at(MON, 7, 50))
    fb = json.loads(box.sent[0][2])
    assert not fb["problem"].startswith("【給 Grok】") and "GROK_DAILY 規則" not in fb["how_to_fix"]


def test_longest_grok_fix_text_fits_feedback_limit():
    reason = "schema_invalid " + "; ".join(f"GROK_DAILY.events.{i}.urls.0:url_too_long" for i in range(5))
    assert len(muse_ops.fix_for(reason)) < 1000
    assert "GROK_DAILY 規則" in muse_ops.fix_for("schema_invalid GROK_DAILY:value_error")


def test_grok_consistency_reject_names_the_rule(con):
    """A model-level ValueError used to be stored as a bare GROK_DAILY:value_error,
    so neither the sender nor the review could tell which rule failed."""
    box = FakeMailbox()
    d = message_for(MON)
    d["tracked_accounts"] = ["@trader_abc", "@trader_abc"]
    deliver(box, d)
    assert poll(con, box, CONFIG, now=at(MON, 7, 45)) == 0
    reason = con.execute("SELECT reason FROM bridge_rejects").fetchone()[0]
    assert "value_error (tracked_accounts has duplicates)" in reason
    assert "GROK_DAILY 規則" in muse_ops.fix_for(reason)

# ---------------------------------------------------------------- evening digest (20:00): stored, never scored

def test_evening_message_same_day_does_not_count_as_the_morning_delivery(con):
    grok_on(con)
    box = FakeMailbox()
    deliver(box, message_for(MON, "grok-20261012-2000", hour=20))
    assert poll(con, box, CONFIG, now=at(MON, 20, 15)) == 1  # stored
    # the first check of the day runs late (the Mac slept through the morning)
    assert muse_ops.check_deliveries(con, at(MON, 21)) == ["Grok 今天沒有準時送出「Grok 盤前日報」(期限 08:30 ET)" + MISS_NOTE]
    assert muse_ops.scorecard(con, at(MON, 23), days=1)["deliveries"]["GROK_AM"]["on_time"] == 0


def test_missing_evening_message_never_alerts(con):
    grok_on(con)
    box = FakeMailbox()
    deliver(box, message_for(MON))
    poll(con, box, CONFIG, now=at(MON, 7, 45))
    for h in (9, 20, 21, 23):
        assert muse_ops.check_deliveries(con, at(MON, h, 59)) == []
    assert all(k == "GROK_AM" for k, e in muse_ops.EXPECTATIONS.items() if e.type == "GROK_DAILY")


@pytest.mark.parametrize("received", [(-1, 20, 15), (0, 4, 30)])  # last night, or delayed past midnight
def test_previous_evening_message_does_not_satisfy_this_morning(con, received):
    grok_on(con)
    friday_evening = message_for(MON - timedelta(days=3), "grok-20261009-2000", hour=20)
    days, h, m = received
    box = FakeMailbox()
    deliver(box, friday_evening)
    poll(con, box, CONFIG, now=at(MON + timedelta(days=days), h, m))
    assert len(muse_ops.check_deliveries(con, at(MON, 8, 30))) == 1


def test_morning_and_evening_both_stored_morning_scored(con):
    grok_on(con)
    box = FakeMailbox()
    deliver(box, message_for(MON))
    poll(con, box, CONFIG, now=at(MON, 7, 45))
    deliver(box, message_for(MON, "grok-20261012-2000", hour=20))
    poll(con, box, CONFIG, now=at(MON, 20, 15))
    assert con.execute("SELECT COUNT(*) FROM bridge_messages WHERE type='GROK_DAILY'").fetchone()[0] == 2
    assert muse_ops.scorecard(con, at(MON, 23), days=1)["deliveries"]["GROK_AM"] == {
        "name": "Grok 盤前日報", "on_time": 1, "due": 1}


def test_morning_collected_after_noon_does_not_count(con):
    grok_on(con)
    box = FakeMailbox()
    d = message_for(MON)
    d["collected_at"] = "2026-10-12T12:00:00-04:00"
    d["sent_at"] = "2026-10-12T12:05:00-04:00"
    deliver(box, d)
    poll(con, box, CONFIG, now=at(MON, 12, 10))
    assert len(muse_ops.check_deliveries(con, at(MON, 12, 15))) == 1
