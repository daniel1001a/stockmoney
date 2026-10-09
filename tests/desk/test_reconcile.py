from stockmoney.desk.execution.reconcile import reconcile
from stockmoney.desk.execution.types import AccountSnapshot, AssetClass, BrokerPosition, Side


def snap(*positions):
    return AccountSnapshot(equity=50_000, cash=50_000, buying_power=50_000, positions=list(positions))


def bp(symbol, qty, asset=AssetClass.EQUITY, price=100.0):
    return BrokerPosition(symbol, asset, qty, price, qty * price, price)


def test_in_sync_returns_empty(ledger, hold, broker):
    hold("AAPL", "breakout", 10)
    hold("BTC/USD", "crypto", 0.25)
    assert reconcile(ledger, broker.account()) == []


def test_empty_both_sides(ledger):
    assert reconcile(ledger, snap()) == []


def test_equity_quantity_mismatch(ledger, hold, broker):
    hold("AAPL", "breakout", 10)
    broker.positions["AAPL"].qty = 9
    out = reconcile(ledger, broker.account())
    assert len(out) == 1 and "AAPL" in out[0] and "mismatch" in out[0]


def test_equity_is_exact_not_tolerant(ledger, hold, broker):
    hold("AAPL", "breakout", 10)
    broker.positions["AAPL"].qty = 10.0000001
    assert len(reconcile(ledger, broker.account())) == 1


def test_crypto_tolerates_1e_6(ledger, hold, broker):
    hold("BTC/USD", "crypto", 0.25)
    broker.positions["BTC/USD"].qty = 0.25 + 5e-7
    assert reconcile(ledger, broker.account()) == []
    broker.positions["BTC/USD"].qty = 0.25 + 5e-6
    out = reconcile(ledger, broker.account())
    assert len(out) == 1 and "BTC/USD" in out[0]


def test_broker_position_unknown_to_ledger(ledger):
    out = reconcile(ledger, snap(bp("TSLA", 5)))
    assert len(out) == 1 and "TSLA" in out[0] and "ledger has no such position" in out[0]


def test_ledger_position_missing_at_broker(ledger):
    ledger.apply_fill(client_order_id="a", symbol="AAPL", strategy="breakout", side=Side.BUY, qty=10, price=100.0)
    out = reconcile(ledger, snap())
    assert len(out) == 1 and "AAPL" in out[0] and "broker has no such position" in out[0]


def test_crypto_symbol_spelling_is_normalized(ledger):
    ledger.apply_fill(client_order_id="a", symbol="BTC/USD", strategy="crypto", side=Side.BUY, qty=0.1, price=50_000)
    assert reconcile(ledger, snap(bp("BTCUSD", 0.1, AssetClass.CRYPTO, 50_000))) == []


def test_multiple_mismatches_all_reported(ledger, hold, broker):
    hold("AAPL", "breakout", 10)
    hold("MSFT", "pullback", 5)
    broker.positions["AAPL"].qty = 8
    del broker.positions["MSFT"]
    broker.positions["NVDA"] = bp("NVDA", 3)
    out = reconcile(ledger, broker.account())
    assert len(out) == 3
    assert any("AAPL" in m for m in out) and any("MSFT" in m for m in out) and any("NVDA" in m for m in out)


def test_accepts_raw_connection(con, broker, hold):
    hold("AAPL", "breakout", 10)
    assert reconcile(con, broker.account()) == []
