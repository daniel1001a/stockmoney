"""Read-only check of the option and stock market data the options sleeve uses.

For SPY and NVDA prints: the nearest expiry and its chain size, how many
contracts carry a two-sided quote / IV / delta / open interest, one sample
contract near the money, and the last 5 one-minute bars of the stock and of
the sample contract. Also probes which data feeds the account actually gets
(options: opra vs indicative; stocks: iex vs sip) and the account's options
trading level.

Never places, modifies or cancels an order: the broker's order methods are
replaced with a function that raises before anything runs. Paper keys only.

Usage:
    PYTHONPATH=src python scripts/options_data_check.py [--env-file PATH]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from stockmoney.desk.execution.alpaca import AlpacaBroker, LiveTradingNotEnabled, MissingCredentials

UNDERLYINGS = ("SPY", "NVDA")


def _no_orders(*_a, **_k):
    raise RuntimeError("options_data_check is read-only: order methods are disabled")


def make(**kw) -> AlpacaBroker:
    b = AlpacaBroker(**kw)
    if not b.is_paper:
        raise LiveTradingNotEnabled("options_data_check only runs against the paper environment")
    for name in ("submit", "cancel", "cancel_all"):
        setattr(b, name, _no_orders)
    return b


def fmt(x, nd=2) -> str:
    return "None" if x is None else f"{x:.{nd}f}"


def last_bars(b: AlpacaBroker, symbol: str, now: datetime):
    """The last 5 one-minute bars within the past 4 days (the market may be closed)."""
    df = b.minute_bars(symbol, now - timedelta(days=4), now)
    return df.tail(5)


def probe_option_feeds(occ: str) -> None:
    for feed in ("opra", "indicative"):
        try:
            k = make(option_feed=feed).option_quote(occ)
            print(f"  option feed {feed:<10}: OK  bid={fmt(k.bid if k else None)} ask={fmt(k.ask if k else None)} "
                  f"as_of={k.as_of if k else None}")
        except Exception as e:
            print(f"  option feed {feed:<10}: NOT AVAILABLE ({type(e).__name__}: {str(e)[:160]})")


def probe_stock_feeds(now: datetime) -> None:
    for feed in ("iex", "sip"):
        try:
            b = make(stock_feed=feed)
            q = b.stock_quote("SPY")
            recent = b.minute_bars("SPY", now - timedelta(minutes=30), now)
            print(f"  stock feed {feed:<4}: quote={q} bars_last_30min={len(recent)}")
        except Exception as e:
            print(f"  stock feed {feed:<4}: NOT AVAILABLE ({type(e).__name__}: {str(e)[:160]})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default=None, help="dotenv file with the paper keys (default: search for .env)")
    args = ap.parse_args()
    load_dotenv(args.env_file) if args.env_file else load_dotenv()
    try:
        b = make()
    except (MissingCredentials, LiveTradingNotEnabled, ValueError) as e:
        print(f"options_data_check: {e}", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc)
    try:
        acct = b.client.get_account()
        clock = b.client.get_clock()
    except Exception as e:
        print(f"options_data_check: request failed ({type(e).__name__}): {e}", file=sys.stderr)
        return 1
    print(f"broker: {b.name} (paper={b.is_paper})  now={now:%Y-%m-%d %H:%M:%S} UTC  market_open={clock.is_open}")
    print(f"options: approved_level={acct.options_approved_level} trading_level={acct.options_trading_level} "
          f"options_buying_power={acct.options_buying_power}")
    print(f"configured option feed: {b.option_feed or 'auto (Alpaca default)'}; stock feed: {b.stock_feed}")
    sample_occ = None
    for u in UNDERLYINGS:
        print(f"\n== {u}")
        # after the close, today's expiry is finished: look from the next session on
        today = now.date() if clock.is_open else clock.next_open.date()
        try:
            chain = b.option_chain(u, today, today + timedelta(days=10))
        except Exception as e:
            print(f"  chain failed: {type(e).__name__}: {e}")
            continue
        if not chain:
            print("  chain is empty")
            continue
        nearest = min(c.expiry for c in chain)
        near = [c for c in chain if c.expiry == nearest]
        n = len(near)
        print(f"  nearest expiry {nearest}: {n} contracts "
              f"(window {today}..{today + timedelta(days=10)}: {len(chain)} contracts)")
        print(f"  two-sided quote: {sum(c.mid is not None for c in near)}/{n}  iv: {sum(c.iv is not None for c in near)}/{n}"
              f"  delta: {sum(c.delta is not None for c in near)}/{n}  open_interest: "
              f"{sum(c.open_interest is not None for c in near)}/{n}  volume>0: {sum((c.volume or 0) > 0 for c in near)}/{n}")
        quote = b.stock_quote(u)
        spot = (quote[0] + quote[1]) / 2 if quote else b.latest_price(u)
        print(f"  stock quote (bid, ask): {quote}  spot used: {fmt(spot)}")
        calls = [c for c in near if c.right == "C"]
        if spot and calls:
            k = min(calls, key=lambda c: abs(c.strike - spot))
            print(f"  sample {k.occ}: bid={fmt(k.bid)} ask={fmt(k.ask)} mid={fmt(k.mid, 3)} "
                  f"spread={fmt(k.spread_pct and k.spread_pct * 100, 1)}% last={fmt(k.last)} iv={fmt(k.iv, 4)} "
                  f"delta={fmt(k.delta, 3)} oi={k.open_interest} volume={k.volume} mult={k.multiplier} as_of={k.as_of}")
            sample_occ = sample_occ or k.occ
            for sym in (u, k.occ):
                try:
                    bars = last_bars(b, sym, now)
                    print(f"  last 5 one-minute bars of {sym}:")
                    print("    " + (bars.to_string().replace("\n", "\n    ") if not bars.empty else "(none)"))
                except Exception as e:
                    print(f"  bars for {sym} failed: {type(e).__name__}: {e}")
    if sample_occ:
        print("\n== option feed probe")
        probe_option_feeds(sample_occ)
    print("\n== stock feed probe")
    probe_stock_feeds(now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
