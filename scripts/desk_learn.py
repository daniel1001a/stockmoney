"""Learning layer CLI (read-only for orders; writes only learning tables).

    desk_learn.py run [--shadow-start YYYY-MM-DD] [--allocator]   run the daily learning job once
    desk_learn.py cards [--limit 20]                              list post-mortem cards
    desk_learn.py lesson TRADE_ID "text"                          write your one-line lesson
    desk_learn.py chain add FILE.json [--source owner|claude]     record an event chain (JSON object)
    desk_learn.py chain list | report
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from stockmoney.desk import store
from stockmoney.desk.learn import chains, job, tables


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--shadow-start")
    r.add_argument("--allocator", action="store_true")
    c = sub.add_parser("cards")
    c.add_argument("--limit", type=int, default=20)
    le = sub.add_parser("lesson")
    le.add_argument("trade_id")
    le.add_argument("text")
    ch = sub.add_parser("chain")
    ch.add_argument("action", choices=["add", "list", "report"])
    ch.add_argument("file", nargs="?")
    ch.add_argument("--source", default="owner")
    a = ap.parse_args()
    with store.connect() as con:
        tables.ensure_schema(con)
        if a.cmd == "run":
            print(job.run(con, datetime.now(timezone.utc), shadow_start=a.shadow_start,
                          with_allocator=True if a.allocator else None))
        elif a.cmd == "cards":
            for x in con.execute("SELECT * FROM trade_postmortem ORDER BY exit_day DESC LIMIT ?", (a.limit,)):
                print(f"{x['trade_id']} {x['exit_day']} {x['symbol']:6} {x['strategy']:9} ret {x['ret']:+.1%} "
                      f"R {x['r_multiple'] if x['r_multiple'] is not None else float('nan'):+.2f} "
                      f"[{x['symptoms']}] {x['status']} {x['lesson']}")
        elif a.cmd == "lesson":
            con.execute("UPDATE trade_postmortem SET lesson=? WHERE trade_id=?", (a.text, a.trade_id))
            print("ok")
        else:
            if a.action == "add":
                obj = json.loads(Path(a.file).read_text())
                obj.setdefault("source", a.source)
                print(chains.add_chain(con, chains.Chain.model_validate(obj)))
            elif a.action == "list":
                for x in con.execute("SELECT chain_id, created_at, source, trigger_text, confidence FROM chains "
                                     "ORDER BY created_at DESC LIMIT 30"):
                    print(f"{x['chain_id']} {x['created_at'][:16]} {x['source']:6} {x['confidence']:.2f} {x['trigger_text']}")
            else:
                print(chains.report(con))
    return 0


if __name__ == "__main__":
    sys.exit(main())
