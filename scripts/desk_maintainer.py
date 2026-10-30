"""Read-only parameter registry for the paper rule maintainer."""
from __future__ import annotations

import argparse
from pathlib import Path

from stockmoney.desk import store
from stockmoney.desk.learn.registry import REGISTRY, current


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", action="store_true", required=True)
    parser.add_argument("--db", type=Path, default=store.DEFAULT_PATH)
    args = parser.parse_args(argv)
    with store.connect(args.db) as con:
        print("key\tsleeve\tcurrent\tbound\tmax_step\tai_tunable")
        for key, item in sorted(REGISTRY.items()):
            print(f"{key}\t{item.sleeve}\t{current(con, key)}\t{item.bound.describe()}"
                  f"\t{item.max_step:g}\t{item.ai_tunable}")


if __name__ == "__main__":
    main()
