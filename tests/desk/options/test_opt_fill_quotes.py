"""A live fill row carries the broker's fill time and the quote its order
was sent against (arrival price). Before 2026-10-21 the entry row was stamped
with the tick start and the exit row with last_bid/ask, which later polls
overwrite, so exit slippage was measured against a quote taken after the fill.
Fake broker only: no real order is ever sent."""
import pytest

from stockmoney.desk import store
from stockmoney.desk.options import _iface
from stockmoney.desk.options import store as ostore
from stockmoney.desk.options._iface import OrderStatus

from opt_fakes import et
from test_opt_engine import Rig
from test_opt_review_fixes import C601N, exits


@pytest.fixture
def con(tmp_path):
    with store.connect(tmp_path / "desk.sqlite") as c:
        yield c


def test_entry_fill_at_is_the_broker_fill_time_and_carries_the_send_quote(tmp_path, con):
    r = Rig(tmp_path, con)
    r.run(et(9, 52))
    lp = r.pos("live")[0]
    row = ostore.order(con, ostore.cid(lp["position_id"], "e"))
    o = r.broker.orders[row["client_order_id"]]
    buy = [f for f in ostore.fills(con, lp["position_id"]) if f["side"] == "buy"][0]
    assert buy["at"] == ostore.iso(o.filled_at)
    assert (buy["bid"], buy["ask"]) == (row["ref_bid"], row["ref_ask"]) == (lp["entry_bid"], lp["entry_ask"])
    assert row["ref_quote_at"] is not None and buy["quote_at"] == row["ref_quote_at"]


def test_exit_fill_uses_the_quote_at_send_not_a_later_poll(tmp_path, con):
    r = Rig(tmp_path, con)
    r.gate = _iface.make_risk_gate(con, r.broker, r.cfg)
    r.eng = r.new_engine()
    r.run(et(9, 52))
    lp = r.pos("live")[0]
    r.broker.fill = "never"
    r.broker.mult[C601N] = 0.6  # premium stop
    while not exits(con, lp["position_id"]):
        r.eng.tick()
        r.clock.advance(2)
    ex = exits(con, lp["position_id"])[0]
    sent_bid, sent_ask = ex["ref_bid"], ex["ref_ask"]
    assert sent_bid is not None and sent_ask is not None
    filled_at = r.clock.now
    # the quote moves after the sell was sent; the next polls overwrite last_bid/ask
    r.broker.mult[C601N] = 0.5
    r.clock.advance(2)
    r.eng.tick()
    assert ostore.position(con, lp["position_id"])["last_bid"] != sent_bid
    o = r.broker.orders[ex["client_order_id"]]
    o.status, o.filled_qty, o.filled_avg_price, o.filled_at = OrderStatus.FILLED, o.qty, sent_bid, filled_at
    del r.broker.positions[C601N]
    r.clock.advance(2)
    r.eng.tick()
    sell = [f for f in ostore.fills(con, lp["position_id"]) if f["side"] == "sell"][0]
    assert (sell["bid"], sell["ask"]) == (sent_bid, sent_ask)
    assert sell["at"] == ostore.iso(filled_at)
    assert sell["quote_at"] == ex["ref_quote_at"]
    assert ostore.position(con, lp["position_id"])["exit_time"] == ostore.iso(filled_at)


def test_exit_without_a_send_quote_falls_back_to_last_quote(tmp_path, con):
    """A forced market sell is sent with no quote: the row has no ref_*, so the
    fill is measured against the last polled quote, as before."""
    r = Rig(tmp_path, con)
    r.run(et(9, 52))
    lp = r.pos("live")[0]
    p = ostore.position(con, lp["position_id"])
    r.broker.fill = "never"
    assert r.eng._send_exit(p, p["qty_open"], "force_flat", None, r.clock.now, force_market=True)
    ex = exits(con, lp["position_id"])[0]
    assert ex["ref_bid"] is None and ex["ref_ask"] is None
    o = r.broker.orders[ex["client_order_id"]]
    o.status, o.filled_qty, o.filled_avg_price, o.filled_at = OrderStatus.FILLED, o.qty, 0.9, r.clock.now
    del r.broker.positions[C601N]
    r.clock.advance(2)
    r.eng.tick()
    sell = [f for f in ostore.fills(con, lp["position_id"]) if f["side"] == "sell"][0]
    assert sell["bid"] is not None and sell["ask"] is not None  # the last polled quote
    assert sell["quote_at"] is None
