"""Read-only Alpaca connectivity check: account, clock, positions, open orders.

Never places, modifies or cancels an order. Exits 2 with a message when the
keys are missing or the live-trading guard blocks construction.

Usage:
    uv run python scripts/alpaca_check.py
"""
from __future__ import annotations

import sys

from dotenv import load_dotenv

from stockmoney.desk.execution.alpaca import (
    AlpacaBroker, BrokerError, LiveTradingNotEnabled, MissingCredentials,
)


def main() -> int:
    load_dotenv()
    try:
        broker = AlpacaBroker()
    except MissingCredentials as e:
        print(f"alpaca_check: {e}. Set them in the environment or .env (paper keys).", file=sys.stderr)
        return 2
    except LiveTradingNotEnabled as e:
        print(f"alpaca_check: {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"alpaca_check: bad configuration: {e}", file=sys.stderr)
        return 2

    try:
        snap = broker.account()
        clock = broker.client.get_clock()
        open_orders = broker.open_orders()
    except Exception as e:  # connectivity / auth problems: report, do not traceback
        print(f"alpaca_check: request failed ({type(e).__name__}): {e}", file=sys.stderr)
        return 1

    print(f"broker: {broker.name} (paper={broker.is_paper})")
    print(f"account: equity={snap.equity:,.2f} cash={snap.cash:,.2f} buying_power={snap.buying_power:,.2f} "
          f"trading_blocked={snap.trading_blocked}")
    print(f"clock: open={clock.is_open} next_open={clock.next_open} next_close={clock.next_close}")
    print(f"positions ({len(snap.positions)}):")
    for p in snap.positions:
        print(f"  {p.symbol:<10} {p.asset_class.value:<9} qty={p.qty:g} avg={p.avg_entry_price:,.4f} "
              f"price={p.current_price:,.4f} mv={p.market_value:,.2f}")
    print(f"open orders ({len(open_orders)}):")
    for o in open_orders:
        print(f"  {o.client_order_id} {o.side.value} {o.symbol} qty={o.qty} {o.order_type.value}/{o.tif.value} "
              f"status={o.status.value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
