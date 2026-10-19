"""Daily broker-vs-plan reconcile summary (read-only for orders).

    desk_reconcile.py [--date YYYY-MM-DD] [--no-record] [--no-broker]   summarize one day
    desk_reconcile.py --list                                            recorded history, newest first

Compares Alpaca paper fills and open stop orders with what the desk planned (slippage vs the
plan reference price, planned orders that did not fill, ledger stop vs broker stop). It never
halts, never submits and never changes an order. Postclose runs the same summary every day.
Without --no-broker it reads the broker's open orders (read-only); crypto fills that land after
the 16:45 postclose show up when this is re-run for that date.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from stockmoney.desk import store
from stockmoney.desk.execution import reconcile


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="ET session day, default today")
    ap.add_argument("--no-record", action="store_true", help="print only, write no reconcile_daily row")
    ap.add_argument("--no-broker", action="store_true", help="skip the broker call (the stop-order check is skipped)")
    ap.add_argument("--list", action="store_true", help="show recorded days and exit")
    a = ap.parse_args()
    with store.connect() as con:
        if a.list:
            for h in reconcile.history(con, 30):
                print(f"{h['day']} fills {h['n_fills']} orders {h['n_orders']} slip {h['n_slip_flag']} "
                      f"missed {h['n_missed']} partial {h['n_partial']} stop {h['n_stop_diff']} "
                      f"zero_diff {h['zero_diff']}")
            return 0
        day = a.date or datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        open_orders = None
        if not a.no_broker:
            from stockmoney.desk import run
            open_orders = run.make_broker().open_orders()
        text, alert = reconcile.daily(con, day, open_orders, record_rows=not a.no_record)
        con.commit()
        print(text)
        if alert:
            print("\n[警示] 已達連續差異門檻")
    return 0


if __name__ == "__main__":
    sys.exit(main())
