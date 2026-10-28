"""Jev prescreen of stored Muse/Grok items (writes only prescreen_judgments).

    uv run python scripts/desk_prescreen.py run [--mode shadow|on] [--max-items 200] [--max-deep 10]
    uv run python scripts/desk_prescreen.py report
    uv run python scripts/desk_prescreen.py items [--limit 20]    list pending items, no API call
    uv run python scripts/desk_prescreen.py labels FILE.csv [--source claude]   import importance labels
    uv run python scripts/desk_prescreen.py weekly                 weekly review section, no API call

All subcommands take ``--db PATH`` (default: the desk store) before the subcommand.

``shadow`` judges and logs only; ``on`` also deep-analyses passed items with the
Claude CLI. Needs TYPESAFE_API_KEY for judging. Nothing here can place an order.
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone

from pathlib import Path

from stockmoney.desk import data, store
from stockmoney.desk.learn import prescreen, tables


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=store.DEFAULT_PATH)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--mode", choices=["shadow", "on"], default="shadow")
    r.add_argument("--max-items", type=int, default=prescreen.MAX_JUDGE)
    r.add_argument("--max-deep", type=int, default=prescreen.MAX_DEEP)
    sub.add_parser("report")
    i = sub.add_parser("items")
    i.add_argument("--limit", type=int, default=20)
    lb = sub.add_parser("labels")
    lb.add_argument("csv", type=Path)
    lb.add_argument("--source", default="claude")
    sub.add_parser("weekly")
    a = ap.parse_args()
    with store.connect(a.db) as con:
        tables.ensure_schema(con)
        if a.cmd == "run":
            out = prescreen.step(con, mode=a.mode, max_items=a.max_items, max_deep=a.max_deep)
            con.commit()
            print(json.dumps(out, ensure_ascii=False))
        elif a.cmd == "report":
            print(prescreen.report(con) or "no prescreen data yet")
        elif a.cmd == "labels":
            with a.csv.open(newline="", encoding="utf-8") as f:
                n = prescreen.import_labels(con, list(csv.DictReader(f)), a.source)
            con.commit()
            print(f"imported {n} labels")
        elif a.cmd == "weekly":
            try:
                bars = data.to_bars(data.load())
            except (OSError, ValueError, KeyError):
                bars = None
            print(prescreen.weekly_report(con, bars) or "no prescreen data yet")
        else:
            for it in prescreen.pending_items(con, datetime.now(timezone.utc), a.limit):
                print(f"{it.item_id}\t{it.ticker}\t{it.bypass or '-'}\t{it.text[:100]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
