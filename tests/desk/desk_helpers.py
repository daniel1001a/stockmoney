"""Intent builders shared by the execution-safety tests."""
from stockmoney.desk.execution.types import AssetClass, OrderIntent, Side


def _intent(side, symbol, qty, notional, strategy, asset, cid, **kw):
    asset = asset or (AssetClass.CRYPTO if "/" in symbol else AssetClass.EQUITY)
    return OrderIntent(cid, symbol, asset, side, qty=qty, notional=notional, strategy=strategy, **kw)


def buy(symbol="AAPL", qty=None, notional=None, strategy="breakout", asset=None, cid="c1", **kw):
    return _intent(Side.BUY, symbol, qty, notional, strategy, asset, cid, **kw)


def sell(symbol="AAPL", qty=None, notional=None, strategy="breakout", asset=None, cid="s1", **kw):
    return _intent(Side.SELL, symbol, qty, notional, strategy, asset, cid, **kw)
