"""Shared fixtures for the execution-safety tests (risk / ledger / reconcile).
Everything runs offline against FakeBroker and a temp SQLite file."""
import pytest

from stockmoney.desk import store
from stockmoney.desk.execution.broker import FakeBroker
from stockmoney.desk.execution.ledger import Ledger
from stockmoney.desk.execution.risk import Limits, RiskEngine
from stockmoney.desk.execution.types import AssetClass, OrderIntent, Side


@pytest.fixture(autouse=True)
def legacy_desk_defaults(monkeypatch):
    """Offline, pre-2026-10-27 behavior unless a test says otherwise: stock-sleeve
    entries on (run.STOCK_ENTRIES_DEFAULT) and no TypeSafe client for the veto's
    Jev catalyst filter (every flagged item counts, no network)."""
    from stockmoney.desk import run, veto
    monkeypatch.setattr(run, "STOCK_ENTRIES_DEFAULT", True)
    monkeypatch.setattr(veto, "jev_client", lambda: None)


@pytest.fixture
def con(tmp_path):
    with store.connect(tmp_path / "desk.sqlite") as c:
        yield c


@pytest.fixture
def halt_file(tmp_path):
    return tmp_path / "HALT"  # not created: kill switch off


@pytest.fixture
def make_engine(con, halt_file):
    def _make(**overrides):
        return RiskEngine(Limits(halt_file=halt_file, **overrides), con)
    return _make


@pytest.fixture
def ledger(con):
    return Ledger(con)


@pytest.fixture
def broker():
    return FakeBroker(cash=50_000.0, prices={
        "AAPL": 100.0, "MSFT": 100.0, "NVDA": 100.0, "BTC/USD": 50_000.0, "ETH/USD": 2_000.0})


@pytest.fixture
def hold(broker, ledger):
    """Open a position in both the fake broker and the ledger."""
    n = iter(range(1, 10_000))

    def _hold(symbol, strategy, qty, price=None):
        price = price if price is not None else broker.prices[symbol]
        asset = AssetClass.CRYPTO if "/" in symbol else AssetClass.EQUITY
        cid = f"seed-{next(n)}"
        broker.prices[symbol] = price
        o = broker.submit(OrderIntent(cid, symbol, asset, Side.BUY, qty=qty, strategy=strategy))
        assert o.filled_qty == qty
        ledger.apply_fill(client_order_id=cid, symbol=symbol, strategy=strategy, side=Side.BUY, qty=qty, price=price)
    return _hold


@pytest.fixture
def realize(ledger):
    """Book a closed round trip in the ledger only: P&L = pnl."""
    n = iter(range(1, 10_000))

    def _realize(strategy, pnl, symbol="ZZZ", qty=1000.0, entry=100.0):
        i = next(n)
        ledger.apply_fill(client_order_id=f"rt-b{i}", symbol=symbol, strategy=strategy,
                          side=Side.BUY, qty=qty, price=entry)
        ledger.apply_fill(client_order_id=f"rt-s{i}", symbol=symbol, strategy=strategy,
                          side=Side.SELL, qty=qty, price=entry + pnl / qty)
    return _realize
