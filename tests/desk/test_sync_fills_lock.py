"""Stock sync_fills and a locked database (options review round 2, R3): a
lock timeout AFTER Ledger.apply_fill committed a fill is retryable (no
halt), and the retry restores position_meta from the ledger state even
though apply_fill then reports the fill as already booked."""
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from stockmoney.desk import run as drun
from stockmoney.desk import store
from stockmoney.desk.execution.ledger import Ledger
from stockmoney.desk.execution.types import TIF, AssetClass, BrokerOrder, OrderStatus, OrderType, Side

ET = ZoneInfo("America/New_York")


class LockOnce:
    """Connection proxy: the first statement matching ``pat`` raises 'database is locked'."""
    def __init__(self, con, pat):
        self._c, self.pat, self.fired = con, pat, False

    def execute(self, sql, *a):
        if not self.fired and self.pat in sql:
            self.fired = True
            raise sqlite3.OperationalError("database is locked")
        return self._c.execute(sql, *a)

    def __enter__(self):
        return self._c.__enter__()

    def __exit__(self, *a):
        return self._c.__exit__(*a)

    def __getattr__(self, n):
        return getattr(self._c, n)


class B:
    def __init__(self, o):
        self.o = o

    def get_order(self, cid):
        return self.o


def _setup(c, cid, side):
    drun.ensure_schema(c)
    c.execute("INSERT INTO submitted (client_order_id, kind, symbol, asset_class, strategy, side, qty, stop, "
              "submitted_at, status, recorded) VALUES (?,?,?,?,?,?,?,?,?,?,0)",
              (cid, "entry" if side == "buy" else "exit", "AAPL", "equity", "trend", side, 10, 140.0,
               datetime.now(ET).isoformat(), "new"))
    c.commit()


def _order(cid, side, px):
    now = datetime.now(timezone.utc)
    return BrokerOrder("1", cid, "AAPL", Side(side), OrderStatus.FILLED, 10, 10, px, now, now, OrderType.MARKET,
                       TIF.DAY, asset_class=AssetClass.EQUITY)


def test_lock_after_ledger_commit_position_meta_restored_on_retry(tmp_path):
    with store.connect(tmp_path / "s.sqlite") as c:
        _setup(c, "b1", "buy")
        proxy = LockOnce(c, "INSERT INTO position_meta")
        ev1 = drun.sync_fills(proxy, B(_order("b1", "buy", 150.0)), Ledger(proxy), "2026-10-13")
        assert proxy.fired and "資料庫忙碌" in ev1[0]
        assert Ledger(c).position("AAPL") is not None  # the fill itself was committed
        assert c.execute("SELECT 1 FROM position_meta WHERE symbol='AAPL'").fetchone() is None
        drun.sync_fills(c, B(_order("b1", "buy", 150.0)), Ledger(c), "2026-10-13")
        meta = c.execute("SELECT * FROM position_meta WHERE symbol='AAPL'").fetchone()
        assert meta is not None and meta["initial_stop"] == 140.0
        assert store.get_setting(c, "halt_new_entries", "0") != "1"


def test_lock_after_full_exit_commit_meta_removed_on_retry(tmp_path):
    with store.connect(tmp_path / "s.sqlite") as c:
        _setup(c, "b1", "buy")
        drun.sync_fills(c, B(_order("b1", "buy", 150.0)), Ledger(c), "2026-10-13")
        assert c.execute("SELECT 1 FROM position_meta WHERE symbol='AAPL'").fetchone()
        c.execute("INSERT INTO submitted (client_order_id, kind, symbol, asset_class, strategy, side, qty, stop, "
                  "submitted_at, status, recorded) VALUES ('s1','exit','AAPL','equity','trend','sell',10,NULL,?,"
                  "'new',0)", (datetime.now(ET).isoformat(),))
        c.commit()
        proxy = LockOnce(c, "DELETE FROM position_meta")
        drun.sync_fills(proxy, B(_order("s1", "sell", 155.0)), Ledger(proxy), "2026-10-13")
        assert proxy.fired and Ledger(c).position("AAPL") is None
        drun.sync_fills(c, B(_order("s1", "sell", 155.0)), Ledger(c), "2026-10-13")
        assert c.execute("SELECT 1 FROM position_meta WHERE symbol='AAPL'").fetchone() is None
