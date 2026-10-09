import pytest

from stockmoney.desk import store
from stockmoney.desk.execution.ledger import Ledger, LedgerError, SleeveConflict
from stockmoney.desk.execution.types import Side


def buy_fill(ledger, cid, symbol, strategy, qty, price, **kw):
    return ledger.apply_fill(client_order_id=cid, symbol=symbol, strategy=strategy, side=Side.BUY,
                             qty=qty, price=price, **kw)


def sell_fill(ledger, cid, symbol, qty, price, strategy=""):
    return ledger.apply_fill(client_order_id=cid, symbol=symbol, strategy=strategy, side=Side.SELL,
                             qty=qty, price=price)


def test_open_position(ledger):
    assert buy_fill(ledger, "a", "AAPL", "breakout", 10, 100.0, stop=90.0)
    pos = ledger.position("AAPL")
    assert (pos.strategy, pos.qty, pos.avg_price, pos.stop) == ("breakout", 10, 100.0, 90.0)
    assert pos.opened_at


def test_add_averages_price_and_keeps_stop(ledger):
    buy_fill(ledger, "a", "AAPL", "breakout", 10, 100.0, stop=90.0)
    buy_fill(ledger, "b", "AAPL", "breakout", 10, 110.0)
    pos = ledger.position("AAPL")
    assert pos.qty == 20 and pos.avg_price == pytest.approx(105.0) and pos.stop == 90.0


def test_one_symbol_one_strategy_raises(ledger):
    buy_fill(ledger, "a", "AAPL", "breakout", 10, 100.0)
    with pytest.raises(SleeveConflict):
        buy_fill(ledger, "b", "AAPL", "pullback", 5, 100.0)
    # nothing was written for the rejected fill
    assert ledger.position("AAPL").qty == 10 and ledger.position("AAPL").strategy == "breakout"
    assert ledger.con.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1


def test_symbol_can_change_strategy_after_full_close(ledger):
    buy_fill(ledger, "a", "AAPL", "breakout", 10, 100.0)
    sell_fill(ledger, "b", "AAPL", 10, 110.0)
    assert ledger.position("AAPL") is None
    assert buy_fill(ledger, "c", "AAPL", "pullback", 5, 120.0)
    assert ledger.owner("AAPL") == "pullback"


def test_partial_and_full_sell_realize_pnl(ledger):
    buy_fill(ledger, "a", "AAPL", "breakout", 10, 100.0)
    sell_fill(ledger, "b", "AAPL", 4, 110.0)
    assert ledger.position("AAPL").qty == 6
    assert ledger.realized_pnl() == pytest.approx(40.0)
    sell_fill(ledger, "c", "AAPL", 6, 90.0)
    assert ledger.position("AAPL") is None
    assert ledger.realized_pnl() == pytest.approx(40.0 - 60.0)
    assert ledger.realized_pnl("breakout") == pytest.approx(-20.0)
    assert ledger.realized_pnl("pullback") == 0.0


def test_sell_attributed_to_position_owner(ledger):
    buy_fill(ledger, "a", "AAPL", "breakout", 10, 100.0)
    sell_fill(ledger, "b", "AAPL", 10, 120.0, strategy="whatever")
    assert ledger.realized_pnl("breakout") == pytest.approx(200.0)


def test_oversell_and_unheld_sell_raise(ledger):
    buy_fill(ledger, "a", "AAPL", "breakout", 10, 100.0)
    with pytest.raises(LedgerError):
        sell_fill(ledger, "b", "AAPL", 11, 100.0)
    with pytest.raises(LedgerError):
        sell_fill(ledger, "c", "MSFT", 1, 100.0)
    assert ledger.position("AAPL").qty == 10


def test_fill_is_idempotent_on_client_order_id(ledger):
    assert buy_fill(ledger, "a", "AAPL", "breakout", 10, 100.0) is True
    assert buy_fill(ledger, "a", "AAPL", "breakout", 10, 100.0) is False
    assert ledger.position("AAPL").qty == 10
    assert sell_fill(ledger, "s", "AAPL", 5, 110.0) is True
    assert sell_fill(ledger, "s", "AAPL", 5, 110.0) is False
    assert ledger.position("AAPL").qty == 5
    assert ledger.realized_pnl() == pytest.approx(50.0)


def test_invalid_fill_rejected(ledger):
    with pytest.raises(LedgerError):
        buy_fill(ledger, "a", "AAPL", "breakout", 0, 100.0)
    with pytest.raises(LedgerError):
        buy_fill(ledger, "b", "AAPL", "breakout", 1, -5.0)
    with pytest.raises(LedgerError):
        buy_fill(ledger, "c", "AAPL", "", 1, 100.0)


def test_unrealized_and_system_equity(ledger):
    buy_fill(ledger, "a", "AAPL", "breakout", 10, 100.0)
    buy_fill(ledger, "b", "BTC/USD", "crypto", 0.5, 40_000.0)
    prices = {"AAPL": 110.0, "BTCUSD": 38_000.0}  # alternate crypto spelling resolves
    assert ledger.unrealized_pnl(prices) == pytest.approx(100.0 - 1000.0)
    assert ledger.unrealized_pnl(prices, "breakout") == pytest.approx(100.0)
    assert ledger.unrealized_pnl({}) == 0.0  # no price: valued at cost
    sell_fill(ledger, "c", "AAPL", 5, 120.0)
    assert ledger.system_equity(50_000, prices) == pytest.approx(50_000 + 100.0 + 50.0 - 1000.0)


def test_stop_update(ledger):
    buy_fill(ledger, "a", "AAPL", "breakout", 10, 100.0)
    ledger.set_stop("AAPL", 95.0)
    assert ledger.position("AAPL").stop == 95.0
    with pytest.raises(LedgerError):
        ledger.set_stop("MSFT", 1.0)


def test_state_persists_across_connections(tmp_path):
    path = tmp_path / "persist.sqlite"
    with store.connect(path) as con:
        led = Ledger(con)
        buy_fill(led, "a", "AAPL", "breakout", 10, 100.0)
        led.set_state("k", "v")
    with store.connect(path) as con:
        led = Ledger(con)
        assert led.position("AAPL").qty == 10
        assert led.get_state("k") == "v"
        assert buy_fill(led, "a", "AAPL", "breakout", 10, 100.0) is False
