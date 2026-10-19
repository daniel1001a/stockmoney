"""Learning layer CLI (read-only for orders; writes only learning tables).

    desk_learn.py run [--shadow-start YYYY-MM-DD] [--allocator]   run the daily learning job once
    desk_learn.py cards [--limit 20]                              list post-mortem cards
    desk_learn.py lesson TRADE_ID "text"                          write your one-line lesson
    desk_learn.py chain add FILE.json [--source owner|claude]     record an event chain (JSON object)
    desk_learn.py chain list | report
    desk_learn.py h1-report [--no-record]                         H1 (wider trailing stop) vs combined; DRAFT in param_changes
    desk_learn.py h2-report [--no-record]                         H2 (earnings entry block) vs combined; never auto-enabled
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from stockmoney.desk import data, store
from stockmoney.desk.learn import chains, h_reports, job, tables


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
    for h in ("h1-report", "h2-report"):
        sub.add_parser(h).add_argument("--no-record", action="store_true", help="print only, write no DRAFT row")
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
        elif a.cmd in ("h1-report", "h2-report"):
            try:
                bars = data.to_bars(data.load())
            except Exception as e:  # no price cache: the report still runs, MFE/MAE shows n/a
                print(f"(no bars, MFE/MAE skipped: {type(e).__name__})", file=sys.stderr)
                bars = None
            print(h_reports.run(con, a.cmd[:2].upper(), bars, record=not a.no_record))
        else:
            if a.action == "add":
                obj = json.loads(Path(a.file).read_text())
                obj.setdefault("source", a.source)
                chain = chains.Chain.model_validate(obj)
                print(chains.add_chain(con, chain) + ("" if chain.falsifiable else
                      "  (unfalsifiable: no close_below/close_above invalidation; excluded from evidence counts)"))
            elif a.action == "list":
                for x in con.execute("SELECT chain_id, created_at, source, trigger_text, confidence FROM chains "
                                     "ORDER BY created_at DESC LIMIT 30"):
                    print(f"{x['chain_id']} {x['created_at'][:16]} {x['source']:6} {x['confidence']:.2f} {x['trigger_text']}")
            else:
                print(chains.report(con))
    return 0


if __name__ == "__main__":
    sys.exit(main())
