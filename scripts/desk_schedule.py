"""Generate (and optionally install) macOS launchd jobs for the desk.

    .venv/bin/python scripts/desk_schedule.py            # write plists to data/desk/launchd/ only
    .venv/bin/python scripts/desk_schedule.py --install  # also copy to ~/Library/LaunchAgents and load
    .venv/bin/python scripts/desk_schedule.py --uninstall

Times are US/Eastern and assume the Mac's clock is set to Eastern time (checked
at install). The Mac must be awake; jobs are idempotent and catch up on the
next run if one is missed.
"""
from __future__ import annotations

import argparse
import plistlib
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.stockmoney.desk.{}"
WEEKDAYS = range(1, 6)  # launchd: 1=Mon .. 5=Fri

JOBS = {
    "premarket": {"times": [(8, 45)], "weekdays": True},   # read Muse, submit market-on-open orders
    "postopen": {"times": [(9, 50)], "weekdays": True},    # sync fills, place protective stops
    "postclose": {"times": [(16, 45)], "weekdays": True},  # reconcile, trail stops, new signals, report
    "crypto": {"times": [(20, 0)], "weekdays": False},     # daily crypto sleeve, 7 days
    "learn": {"times": [(17, 15)], "weekdays": True},      # read-only: post-mortems, shadow books, chain scoring
    "bridge": {"interval": 900},                           # poll Muse every 15 minutes
    "options": {"times": [(9, 0)], "weekdays": True},      # intraday options session, loops 09:20 to 16:00 ET
    # launchd replays a missed calendar job after sleep but not after a power-off;
    # at every load (boot, login, install) recover books fills and restores stops
    "recover": {"run_at_load": True},
}


# bridge needs the Muse mailbox configured; crypto is a Phase 1 placeholder; options is the
# intraday options sleeve (docs/desk/OPTIONS_PLAN.md). All three are opt-in (--jobs ...).
DEFAULT_JOBS = ["premarket", "postopen", "postclose", "recover", "learn"]


def plist(name: str, spec: dict) -> dict:
    d = {
        "Label": LABEL.format(name),
        "ProgramArguments": [str(ROOT / "scripts/desk_job.sh"), name],
        "WorkingDirectory": str(ROOT),
        "StandardOutPath": str(ROOT / f"data/desk/logs/launchd-{name}.out"),
        "StandardErrorPath": str(ROOT / f"data/desk/logs/launchd-{name}.err"),
    }
    if spec.get("run_at_load"):
        d["RunAtLoad"] = True
    elif "interval" in spec:
        d["StartInterval"] = spec["interval"]
    else:
        days = WEEKDAYS if spec["weekdays"] else [None]
        d["StartCalendarInterval"] = [
            {k: v for k, v in {"Weekday": wd, "Hour": h, "Minute": m}.items() if v is not None}
            for wd in days for h, m in spec["times"]
        ]
    return d


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--jobs", default=",".join(DEFAULT_JOBS),
                    help="comma-separated subset of: " + ",".join(JOBS) + f" (default {','.join(DEFAULT_JOBS)})")
    args = ap.parse_args()
    chosen = args.jobs.split(",")
    unknown = set(chosen) - set(JOBS)
    if unknown:
        raise SystemExit(f"unknown job(s): {sorted(unknown)}")
    out = ROOT / "data/desk/launchd"
    out.mkdir(parents=True, exist_ok=True)
    (ROOT / "data/desk/logs").mkdir(parents=True, exist_ok=True)
    agents = Path.home() / "Library/LaunchAgents"
    for name, spec in ((n, JOBS[n]) for n in chosen):
        path = out / f"{LABEL.format(name)}.plist"
        path.write_bytes(plistlib.dumps(plist(name, spec)))
        target = agents / path.name
        if args.uninstall or args.install:
            subprocess.run(["launchctl", "unload", str(target)], capture_output=True)
            target.unlink(missing_ok=True)
        if args.install:
            target.write_bytes(path.read_bytes())
            subprocess.run(["launchctl", "load", str(target)], check=True)
        print(("installed " if args.install else "wrote ") + str(target if args.install else path))
    if args.install and time.strftime("%Z") not in ("EST", "EDT"):
        print(f"WARNING: Mac timezone is {time.strftime('%Z')}, job times assume US/Eastern")


if __name__ == "__main__":
    main()
