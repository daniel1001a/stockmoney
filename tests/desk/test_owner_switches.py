"""Owner switches of 2026-11-25 (stockmoney.desk.switches): GROK_MAIL_ENABLED and
OPTIONS_ENABLED, both off unless set. tests/desk/conftest.py turns both on for
the older tests; every test here starts from the real default (unset)."""
from datetime import datetime, timezone

import pytest

from stockmoney.desk import grok_outbound, muse_ops, switches
from stockmoney.desk.bridge import inbox, outbox
from stockmoney.desk.bridge.mailbox import FakeMailbox
from stockmoney.desk.bridge.schema import Ping, Report
from stockmoney.desk.options import run as orun
from stockmoney.desk.options import spreads

from test_bridge_grok_upgrade import (  # noqa: F401  (fixtures: con, desk, pages)
    CONFIG, GROK, NOW, SENDER, _briefed, alert, booked, con, desk, et, pages, returns, send,
)


@pytest.fixture(autouse=True)
def owner_defaults(monkeypatch):
    monkeypatch.delenv(switches.GROK_MAIL, raising=False)
    monkeypatch.delenv(switches.OPTIONS, raising=False)


@pytest.mark.parametrize("value, on", [(None, False), ("", False), ("0", False), ("off", False), ("no", False),
                                       ("1", True), ("true", True), (" Yes ", True), ("ON", True)])
def test_switch_values(value, on):
    env = {} if value is None else {switches.GROK_MAIL: value, switches.OPTIONS: value}
    assert switches.grok_mail_enabled(env) is on
    assert switches.options_enabled(env) is on


def test_grok_mail_off_sends_nothing_to_grok_all_day(desk, pages):
    """No DESK_BRIEF (06:30), PLAN_REMINDER (08:45) or RESEARCH_REQUEST (17:45),
    even with Grok's address configured and postclose booked."""
    booked(desk)
    box = FakeMailbox()
    for at in (et(6, 30), et(8, 45), et(9, 0), et(17, 45), et(21, 0)):
        assert grok_outbound.run(desk, box, at, CONFIG, returns) == []
    assert box.sent == [] and pages == []
    assert desk.execute("SELECT COUNT(*) FROM bridge_outbox").fetchone()[0] == 0
    # nothing was marked done either: switching it back on resumes the same day
    assert not grok_outbound._done(desk, "brief:2026-10-27")


def test_grok_mail_back_on_resumes(desk, monkeypatch):
    box = FakeMailbox()
    assert grok_outbound.run(desk, box, et(7, 0), CONFIG, returns) == []
    monkeypatch.setenv(switches.GROK_MAIL, "1")
    assert len(grok_outbound.run(desk, box, et(7, 5), CONFIG, returns)) == 1
    assert [to for to, _, _ in box.sent] == [GROK]


def test_outbox_refuses_the_grok_channel_but_still_mails_the_owner():
    box = FakeMailbox()
    ping = Ping(msg_id=outbox.new_msg_id("ping-grok"), sent_at=NOW, nonce="ab" * 8, ask="x")
    with pytest.raises(outbox.GrokMailDisabled):
        outbox.send(box, ping, CONFIG, channel="grok")
    report = Report(msg_id=outbox.new_msg_id("rp"), sent_at=NOW, title="Desk 盤後報告", body_markdown="ok")
    outbox.send(box, report, CONFIG, channel="muse")
    assert [to for to, _, _ in box.sent] == [SENDER]


def test_send_ping_to_grok_is_refused(con):
    box = FakeMailbox()
    with pytest.raises(outbox.GrokMailDisabled):
        muse_ops.send_ping(con, box, "grok", NOW, CONFIG)
    assert box.sent == []


def test_feedback_to_grok_is_held_and_muse_feedback_still_goes(con, pages):
    box = FakeMailbox()
    send(box, alert(mood="x"))  # from Grok's own address
    send(box, alert("alert-muse-0001", mood="x"), sender=SENDER, token=CONFIG.shared_token, auth="")
    inbox.poll(con, box, CONFIG, now=NOW)
    assert muse_ops.send_feedback(con, box, NOW, CONFIG) == 1
    assert [to for to, _, _ in box.sent] == [SENDER]
    assert pages == []  # the held Grok item never counts against the cap


def test_bridge_poll_keeps_running_with_grok_mail_off(desk, monkeypatch):
    """The stock side's bridge poll still reads mail and runs its checks; it just
    sends nothing to Grok and never waits on it."""
    from stockmoney.desk import run
    monkeypatch.setattr(grok_outbound, "ohlcv_returns", returns)
    monkeypatch.setattr(muse_ops, "run_checks", lambda *a, **k: "")
    monkeypatch.setattr(inbox, "poll", lambda *a, **k: 0)
    box = FakeMailbox()
    out = run.bridge(desk, et(7, 0), box, grok_config=CONFIG)
    assert out.startswith("Muse 新訊息 0 則") and "Grok" not in out
    assert box.sent == []


@pytest.mark.parametrize("cmd", ["session", "recover"])
def test_options_off_runs_no_session_and_no_recover(cmd, monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("options sleeve must not start")
    monkeypatch.setattr(orun, "session", boom)
    monkeypatch.setattr(orun, "recover", boom)
    monkeypatch.setattr(orun, "make_session_engine", boom)
    assert orun.main(["run", cmd]) == 0
    assert "OPTIONS_ENABLED is off" in capsys.readouterr().out


def test_options_off_skips_the_spread_shadow(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spread shadow must not tick")
    monkeypatch.setattr(spreads, "tick", boom)
    now = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)  # 11:00 ET, market open
    assert spreads.main(["x", "tick", "--db", str(tmp_path / "d.sqlite")], clock=lambda: now,
                        data_factory=boom) == 0


def test_options_on_still_runs_the_session(monkeypatch):
    called = []
    monkeypatch.setenv(switches.OPTIONS, "1")
    monkeypatch.setattr(orun, "recover", lambda cfg: called.append("recover") or 0)
    assert orun.main(["run", "recover"]) == 0
    assert called == ["recover"]
