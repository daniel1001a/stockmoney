"""Read-only check of stocks-in-play scan candidates for the options sleeve.

For each candidate prints: last close, 20-day average volume, 14-day ATR
(Wilder-free simple mean of true range) from daily bars, and the number of
active option contracts expiring in the next 10 calendar days. A name passes
when price > $5, avg volume >= 1M shares, ATR > $0.50 and near-dated
contracts exist. Names in the starting universe are refused.

Uses only market data reads (daily bars from the IEX feed via
StockHistoricalDataClient, contracts from the options contracts endpoint).
No order method is imported or called. Calls are paced so the run stays far
under Alpaca's per-minute data limit.

Usage:
    PYTHONPATH=src python scripts/options_scan_candidates_check.py [--env-file PATH] [SYM ...]
"""
from __future__ import annotations

import argparse
import sys
import time as _time
from datetime import datetime, timedelta

from dotenv import load_dotenv

DEFAULT = ("HOOD", "SOFI", "MARA", "RIOT", "MSTR", "INTC", "BA", "UBER", "SHOP", "SNOW", "CRWD", "PANW",
           "ORCL", "NKE", "DIS", "BAC", "JPM", "XOM", "PYPL", "RIVN", "ARM", "DELL", "ANET", "MRVL", "QCOM",
           "WMT", "COST", "LLY", "UNH", "CVX", "C", "WFC", "GS", "F", "GM", "NIO", "LCID", "PLUG", "AFRM",
           "UPST", "RBLX", "DKNG", "ABNB", "SQ", "XYZ", "CVNA", "APP", "IONQ", "RGTI", "SMR", "OKLO", "CCJ",
           "VST", "CEG", "GE", "CAT", "IWM", "SNAP", "PINS", "ROKU", "TGT", "CMG", "SBUX", "NEE", "PFE",
           "MRNA", "CLSK", "BITF", "HUT", "CIFR", "WULF", "ACHR", "JOBY", "SOUN", "BBAI", "TSM", "ASML",
           "ADBE", "CRM", "NOW", "INTU", "ON", "LRCX", "AMAT", "KLAC", "WDC", "STX")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--pace", type=float, default=0.75, help="seconds between data calls")
    args = ap.parse_args()
    load_dotenv(args.env_file) if args.env_file else load_dotenv()

    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    from stockmoney.desk.options.config import OptionsConfig
    from stockmoney.desk.options.marketdata import ET, AlpacaOptionsData, bars_frame

    data = AlpacaOptionsData()
    start_set = set(OptionsConfig().starting_universe())
    syms = [s for s in (args.symbols or DEFAULT) if s not in start_set]
    now = datetime.now(ET)
    end = now - timedelta(minutes=20)
    first = end - timedelta(days=45)
    today = now.date()
    print("symbol,close,avg_vol20,atr14,near_contracts,nearest_expiry,pass")
    passed = []
    for s in syms:
        try:
            raw = data.stock.get_stock_bars(StockBarsRequest(symbol_or_symbols=s, timeframe=TimeFrame.Day,
                                                             start=first, end=end, feed=DataFeed.IEX))
            _time.sleep(args.pace)
            d = bars_frame((raw or {}).get(s, []))
            if len(d) < 21:
                print(f"{s},,,,,,no_history({len(d)})")
                continue
            close = float(d["close"].iloc[-1])
            # Volume from the IEX feed is a small slice of the consolidated
            # tape, so the 1M test is run on SIP daily bars (allowed for
            # bars older than 15 minutes).
            raw_sip = data.stock.get_stock_bars(StockBarsRequest(symbol_or_symbols=s, timeframe=TimeFrame.Day,
                                                                 start=first, end=end, feed=DataFeed.SIP))
            _time.sleep(args.pace)
            ds = bars_frame((raw_sip or {}).get(s, []))
            src = ds if len(ds) >= 21 else d
            vol20 = float(src["volume"].iloc[-20:].mean())
            h, lo, pc = src["high"], src["low"], src["close"].shift(1)
            tr = (h - lo).combine((h - pc).abs(), max).combine((lo - pc).abs(), max)
            atr14 = float(tr.iloc[-14:].mean())
            close = float(src["close"].iloc[-1])
            cs = data.contracts(s, today, today + timedelta(days=10))
            _time.sleep(args.pace)
            exps = sorted({c.get("expiration_date") for c in cs if c.get("expiration_date")})
            ok = close > 5 and vol20 >= 1_000_000 and atr14 > 0.5 and len(cs) > 0
            print(f"{s},{close:.2f},{vol20:.0f},{atr14:.2f},{len(cs)},{exps[0] if exps else ''},{ok}")
            if ok:
                passed.append(s)
        except Exception as e:  # one bad symbol never stops the check
            print(f"{s},,,,,,error:{type(e).__name__}:{e}"[:200])
        sys.stdout.flush()
    print("PASSED", len(passed), " ".join(passed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
