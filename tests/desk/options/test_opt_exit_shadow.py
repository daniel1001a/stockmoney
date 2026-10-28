"""Shadow exit model (read only) and the indicative-feed caveat on options reports."""
from datetime import datetime, timedelta, timezone

from stockmoney.desk.options import exit_shadow
from stockmoney.desk.options import report as oreport
from stockmoney.desk.options import store as ostore
from stockmoney.desk.options.config import FEED_CAVEAT, OptionsConfig

DAY = "2026-10-20"
T0 = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)


def _pos(con, pid, mode="live", underlying="SPY", source="orb15", reason="premium_stop", qty=3):
    ostore.insert_position(con, {
        "position_id": pid, "setup_id": "s1", "day": DAY, "mode": mode, "source": source, "underlying": underlying,
        "dte_label": "0dte", "occ": f"{underlying}261020C00600000", "qty_initial": qty, "qty_open": 0,
        "state": "EXITED", "entry_price": 2.0, "exit_reason": reason, "realized_usd": -100.0, "fees_usd": 0.0,
        "exit_variant": "A"})
    return {"position_id": pid, "mode": mode, "underlying": underlying, "source": source}


def _order(con, coid, pid, limit, at, reason, qty, filled=0.0, avg=None, ref_bid=None, ref_ask=None):
    ostore.record_order(con, coid, pid, "exit", "sell", qty, "limit", limit, reason, at, ref_bid=ref_bid, ref_ask=ref_ask)
    ostore.update_order(con, coid, status="filled" if filled else "canceled", filled_qty=filled, avg_price=avg)


def test_filled_first_limit_is_same_under_the_alternative(con):
    ostore.ensure_schema(con)
    p = _pos(con, "p1")
    _order(con, "o1", "p1", 1.59, T0, "premium_stop", 3, filled=3, avg=1.60, ref_bid=1.59, ref_ask=1.63)
    [e] = exit_shadow.compare(con, [p])
    assert e["alt_status"] == "same" and e["alt_minus_live_usd"] == 0
    assert e["chase_usd"] == -3.0  # filled 0.01 above the decision bid on 3 contracts
    assert e["alt_limit"] == 1.57  # half the 0.04 spread below the bid


def test_repriced_exit_fills_at_alt_limit_when_next_bid_reaches_it(con):
    ostore.ensure_schema(con)
    p = _pos(con, "p1")
    _order(con, "o1", "p1", 1.59, T0, "premium_stop", 3, ref_bid=1.59)
    _order(con, "o2", "p1", 1.58, T0 + timedelta(seconds=44), "premium_stop", 3, filled=3, avg=1.50, ref_bid=1.58)
    [e] = exit_shadow.compare(con, [p])
    assert e["orders"] == 2 and e["chase_usd"] == 27.0
    assert e["alt_limit"] == 1.58 and e["alt_status"] == "filled"
    assert e["alt_minus_live_usd"] == 24.0


def test_repriced_exit_is_undetermined_when_next_bid_is_below_alt(con):
    ostore.ensure_schema(con)
    p = _pos(con, "p1")
    _order(con, "o1", "p1", 1.59, T0, "premium_stop", 3)  # older row: no ref_bid, limit is the proxy
    _order(con, "o2", "p1", 1.43, T0 + timedelta(seconds=46), "premium_stop", 3, filled=3, avg=1.43)
    [e] = exit_shadow.compare(con, [p])
    assert e["bid_source"] == "limit" and e["alt_status"] == "undetermined" and e["alt_minus_live_usd"] == 0


def test_take_profit_keeps_the_live_rule_and_shadow_positions_are_skipped(con):
    ostore.ensure_schema(con)
    p = _pos(con, "p1", reason="take_profit", qty=1)
    s = _pos(con, "s1", mode="shadow")
    _order(con, "o1", "p1", 5.34, T0, "take_profit", 1, ref_bid=5.34)
    _order(con, "o2", "p1", 5.34, T0 + timedelta(seconds=40), "take_profit", 1, filled=1, avg=5.32, ref_bid=5.34)
    rows = exit_shadow.compare(con, [p, s])
    assert [r["position_id"] for r in rows] == ["p1"] and rows[0]["alt_status"] == "same"


def test_alt_limit_never_below_one_tick():
    assert exit_shadow.alt_limit("SMCI", 0.01, None, "invalidation") == 0.01
    assert exit_shadow.alt_limit("NVDA", 3.10, None, "invalidation") == 3.05  # $0.05 tick from $3 up


def test_daily_report_carries_feed_caveat_and_exit_section(con):
    ostore.ensure_schema(con)
    _pos(con, "p1")
    _order(con, "o1", "p1", 1.59, T0, "premium_stop", 3, filled=3, avg=1.59, ref_bid=1.59)
    rep = oreport.build(con, DAY, OptionsConfig())
    assert FEED_CAVEAT == "報價為 indicative,滑價量測僅供參考"
    assert FEED_CAVEAT in rep.split("## ")[0]  # at the top, before any section
    assert "## 出場追價(影子出場規則對照,實盤不變)" in rep


def test_daily_report_without_trades_still_carries_caveat(con):
    rep = oreport.build(con, DAY, OptionsConfig())
    assert FEED_CAVEAT in rep and "出場追價" not in rep
