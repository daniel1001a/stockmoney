"""Replay local stock/options history into a tier-labelled evidence result."""
import argparse
import json
from pathlib import Path

from stockmoney.desk import data
from stockmoney.desk.learn import replay


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sleeve", choices=("stocks", "options", "spreads"), required=True)
    parser.add_argument("--from", dest="start", required=True)
    parser.add_argument("--to", dest="end", required=True)
    parser.add_argument("--variant", default="incumbent")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cache", type=Path, default=Path("data/desk/replay.sqlite"))
    parser.add_argument("--options-cache", type=Path, default=Path("data/desk/options/replay_cache"))
    parser.add_argument("--stock-bps", type=float, default=5.0)
    parser.add_argument("--feed", choices=("iex", "sip"), default="iex")
    args = parser.parse_args(argv)
    variant = "incumbent" if args.variant == "incumbent" else json.loads(Path(args.variant).read_text())
    bars = data.to_bars(data.load()) if args.sleeve in ("stocks", "spreads") else None
    result = replay.run(args.sleeve, args.start, args.end, variant, bars=bars, cache_path=args.cache,
                        options_cache=args.options_cache, stock_bps=args.stock_bps, feed=args.feed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
