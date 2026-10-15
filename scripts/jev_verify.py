"""Confirm TypeSafe Jev is working: summarize jev_judgments (shadow log).

Usage: .venv/bin/python scripts/jev_verify.py [--days N] [--ping]
--ping makes one live Jev call (needs TYPESAFE_API_KEY) to prove the key works.
Exit code 1 if there are no judgments in the window or the ping fails.
"""
from __future__ import annotations

import argparse
import sys

import duckdb

from stockmoney.data.db import DEFAULT_DB_PATH


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--ping", action="store_true")
    args = ap.parse_args()

    ok = True
    if args.ping:
        try:
            from stockmoney.data.jev_classify import JEV_MODEL
            from typesafe_sdk import Noul, TypeSafeClient

            r = TypeSafeClient().system_one(
                state={"text": "NVIDIA reported record data center revenue."},
                questions={"q": Noul(instructions="Is `text` about a publicly traded company?")},
                model=JEV_MODEL,
            )
            print(f"ping ok: model={r.model} p(yes)={float(r.answers['q'].noul):.2f}")
        except Exception as exc:
            print(f"ping FAILED: {type(exc).__name__}: {exc}")
            ok = False

    conn = duckdb.connect(str(DEFAULT_DB_PATH), read_only=True)
    rows = conn.execute(
        """
        SELECT route, count(*), round(avg(relevant_p), 2),
               coalesce(sum(input_tokens), 0), coalesce(sum(output_tokens), 0)
        FROM jev_judgments
        WHERE judged_at >= now() - to_days(?)
        GROUP BY 1 ORDER BY 2 DESC
        """,
        [args.days],
    ).fetchall()
    total = sum(r[1] for r in rows)
    print(f"jev_judgments, last {args.days} day(s): {total} total")
    for route, n, rel, tin, tout in rows:
        print(f"  {route:<18} n={n:<5} avg_relevant_p={rel} tokens_in={tin} tokens_out={tout}")
    errs = sum(r[1] for r in rows if r[0] == "jev_error")
    if total == 0:
        print("NO judgments in window: Jev is not running.")
        ok = False
    elif errs:
        print(f"WARNING: {errs} jev_error rows ({errs / total:.0%})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
