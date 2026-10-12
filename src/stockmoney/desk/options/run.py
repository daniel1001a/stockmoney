"""Command line of the intraday options sleeve (OPTIONS_PLAN.md section 8).

    python -m stockmoney.desk.options.run ingest-plan <file>     # Grok's OPTIONS_PLAN JSON
    python -m stockmoney.desk.options.run session                # 09:20 to 16:00 ET loop (launchd starts it 09:00)
    python -m stockmoney.desk.options.run replay YYYY-MM-DD [--feed iex|sip] [--plan FILE] [--step 20]
    python -m stockmoney.desk.options.run report [YYYY-MM-DD]
    python -m stockmoney.desk.options.run selftest               # offline, synthetic day, no network

Session safety:
- its own lock (data/desk/options.lock): two sessions can never run, and it
  never holds the stock jobs' run.lock (it lives all day);
- data/desk/HALT blocks new entries (shadow keeps recording), exits continue;
- real (paper) orders need line A's broker AND risk gate (see _iface.py);
  until they are integrated the session is shadow-only by construction.
  ``DESK_OPTIONS_ORDERS=off`` forces shadow-only even after integration;
- every exit is forced by 15:45 ET and flatness is verified at the end; the
  owner is paged when it is not.
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import os
import signal
import sys
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from stockmoney.desk import notify, store
from stockmoney.desk.calendar import is_trading_day
from stockmoney.desk.options import _iface
from stockmoney.desk.options import plan as oplan
from stockmoney.desk.options import report as oreport
from stockmoney.desk.options import store as ostore
from stockmoney.desk.options.config import OptionsConfig
from stockmoney.desk.options.engine import PENDING, Engine

log = logging.getLogger("desk.options.run")
ET = ZoneInfo("America/New_York")
FAST_POLL_S = 2.0         # while a live order is working (entry cancel after 20 s must be timely)
SESSION_GRACE_S = 1800    # outer alarm: session end + this
_sleep = time.sleep


class SessionTimeout(BaseException):
    """Outer alarm. BaseException so no retry layer can swallow it."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _at(day: date, hm) -> datetime:
    return datetime.combine(day, dtime(*hm), ET)


def _lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def _muse_flags(con):
    """Muse ALERT / risk-flag blocks (risk can only be reduced). Same reader
    the stock book uses; ``held`` is empty because options are intraday."""
    from stockmoney.desk.bridge.policy import active_flags

    def fn(now: datetime) -> dict[str, str]:
        return dict(active_flags(con, now).blocked)
    return fn


def make_session_engine(con, cfg: OptionsConfig, clock=_now) -> tuple[Engine, str]:
    """(engine, mode). Live only when line A's broker and gate are integrated."""
    mode = "paper"
    try:
        broker = _iface.make_trading_broker()
        gate = _iface.make_risk_gate(con)
    except NotImplementedError as e:
        from stockmoney.desk.options.marketdata import AlpacaOptionsData
        broker, gate, mode = AlpacaOptionsData(stock_feed="iex"), _iface.deny_all_gate, "shadow"
        log.warning("shadow-only session: %s", e)
    live = mode == "paper" and os.environ.get("DESK_OPTIONS_ORDERS", "paper").strip().lower() != "off"
    eng = Engine(con, broker, cfg, clock=clock, risk_gate=gate, live=live, flags_fn=_muse_flags(con))
    return eng, ("paper" if eng.live else "shadow")


def session(cfg: OptionsConfig | None = None, db_path: Path = store.DEFAULT_PATH, clock=_now,
            engine_factory=make_session_engine, sleep=None) -> int:
    cfg = cfg or OptionsConfig()
    sleep = sleep or _sleep
    now = clock()
    day = now.astimezone(ET).date()
    if not is_trading_day(day):
        print(f"{day} is not a trading day")
        return 0
    lock = _lock(cfg.lock_path)
    if lock is None:
        msg = "另一個期權 session 仍在執行,本次不啟動。"
        log.error(msg)
        notify.send("期權軌道未啟動", msg, notify.CRITICAL)
        return 1
    try:
        start, end = _at(day, cfg.session_start), _at(day, cfg.session_end)
        while clock() < start:
            sleep(min(30.0, max((start - clock()).total_seconds(), 0.5)))
        with store.connect(db_path) as con:
            ostore.ensure_schema(con)
            eng, mode = engine_factory(con, cfg, clock)
            ap = oplan.active_plan(con, day)
            plan_txt = (f"劇本 {ap.plan.plan_id}({len(ap.plan.plans)} 個,"
                        f"{'只進影子' if ap.shadow_only else '可下紙上單'})") if ap else "沒有 Grok 劇本(規則劇本照跑)"
            halt = "HALT 生效中" if cfg.halt_file.exists() else "HALT 未設"
            notify.send("期權軌道啟動", f"{day} 模式 {mode};{plan_txt};{halt}。", notify.INFO)
            con.commit()
            failures = 0
            try:
                while clock() <= end:
                    try:
                        eng.tick()
                        failures = 0
                    except Exception as e:  # the loop must survive; exits retry next tick
                        failures += 1
                        log.exception("tick failed")
                        if failures == cfg.max_consecutive_errors:
                            notify.send("期權軌道異常", f"連續 {failures} 次迴圈失敗:{type(e).__name__}: {e}",
                                        notify.CRITICAL)
                        con.rollback()
                    working = con.execute("SELECT COUNT(*) FROM options_positions WHERE day=? AND mode='live' "
                                          "AND state=?", (str(day), PENDING)).fetchone()[0]
                    exiting = con.execute("SELECT COUNT(*) FROM options_orders WHERE kind='exit' AND status IN "
                                          "('pending_submit','submitted','new','partially_filled','pending',"
                                          "'pending_cancel') AND position_id LIKE ?", (f"{day}:%",)).fetchone()[0]
                    sleep(FAST_POLL_S if (working or exiting) else cfg.poll_seconds)
            finally:
                flat = _finish(eng, cfg, con, day)
        return 0 if flat else 1
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def _finish(eng: Engine, cfg: OptionsConfig, con, day: date) -> bool:
    """Flatten whatever is still open, verify flat, write the report."""
    try:
        if con.execute("SELECT COUNT(*) FROM options_positions WHERE day=? AND state='ENTERED'",
                       (str(day),)).fetchone()[0]:
            eng.flatten_now("session_end")
        flat = eng.finish()
    except Exception as e:
        log.exception("finish failed")
        notify.send("期權軌道收盤檢查失敗", f"{type(e).__name__}: {e}。請手動確認期權部位是否已全部平倉。", notify.CRITICAL)
        flat = False
    try:
        write_report(con, str(day), cfg)
    except Exception:
        log.exception("report failed")
    return flat


def write_report(con, day: str, cfg: OptionsConfig, header: str = "", name: str | None = None) -> Path:
    text = oreport.build(con, day, cfg, header=header)
    cfg.report_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.report_dir / f"{name or day}.md"
    path.write_text(text, encoding="utf-8")
    return path


def ingest_plan(path: Path, cfg: OptionsConfig, db_path: Path = store.DEFAULT_PATH, now: datetime | None = None) -> int:
    received = now or _now()  # system time at ingest is the desk's clock for the plan
    text = path.read_text(encoding="utf-8")
    with store.connect(db_path) as con:
        res = oplan.ingest(con, text, received, cfg)
    if res.status == "rejected":
        print(f"REJECTED {res.plan_id or '(no id)'}: {res.error}")
        notify.send("Grok 期權劇本被拒收", f"{res.plan_id or '(無 id)'}:{res.error[:500]}", notify.WARN)
        return 1
    note = "(09:20 ET 後收到:當天只進影子帳戶)" if res.shadow_only else ""
    print(f"{res.status.upper()} {res.plan_id} for {res.for_date}{note}")
    return 0


def replay_cmd(day: date, cfg: OptionsConfig, feed: str, plan_file: Path | None, step: float) -> int:
    from stockmoney.desk.options import replay
    cache = cfg.replay_cache / str(day)
    if not (cache / "meta.json").exists():
        from stockmoney.desk.options.marketdata import fetch_replay_cache
        fetch_replay_cache(day, cfg.starting_universe(), cfg.replay_cache,
                           [(b.min_days, b.max_days) for b in cfg.dte_buckets])
    res = replay.run(day, cfg, cfg.replay_cache, cfg.replay_cache.parent / "replay", feed=feed, step_s=step,
                     plan_file=plan_file)
    cfg.report_dir.mkdir(parents=True, exist_ok=True)
    out = cfg.report_dir / f"replay_{day}_{feed}.md"
    out.write_text(res.report, encoding="utf-8")
    print(res.report)
    print(f"report: {out}\ndb: {res.db_path}")
    return 0


def selftest() -> int:
    """Offline end-to-end check on a synthetic day: the example plan
    validates, the engine triggers, enters, exits, ends flat, and a re-run
    of the same day changes nothing."""
    import tempfile

    from stockmoney.desk.options import replay
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        day = date(2026, 10, 13)
        replay.synthetic_cache(tmp / "cache", day)
        # the synthetic day has no option trades, so the volume filter is off here
        cfg = OptionsConfig(report_dir=tmp / "reports", lock_path=tmp / "options.lock", halt_file=tmp / "HALT",
                            min_volume=0)
        plan_file = tmp / "plan.json"
        plan_file.write_text(replay.example_plan(day), encoding="utf-8")
        res = replay.run(day, cfg, tmp / "cache", tmp / "out", feed="sip", step_s=30, plan_file=plan_file)
        with store.connect(res.db_path) as con:
            pos = ostore.positions(con, str(day))
            n_setups = len(ostore.setups(con, str(day)))
            accepted = con.execute("SELECT COUNT(*) FROM options_plans WHERE status='accepted'").fetchone()[0]
        checks = {
            "plan accepted": accepted == 1,
            "setups created": n_setups > 0,
            "shadow positions": any(p["mode"] == "shadow" for p in pos),
            "live positions": any(p["mode"] == "live" for p in pos),
            "all closed": all(p["state"] in ("EXITED", "ABANDONED") for p in pos),
            "report built": "期權短打日報" in res.report,
        }
    for k, v in checks.items():
        print(f"{'ok ' if v else 'FAIL'} {k}")
    return 0 if all(checks.values()) else 1


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="python -m stockmoney.desk.options.run")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest-plan")
    p.add_argument("file", type=Path)
    sub.add_parser("session")
    p = sub.add_parser("replay")
    p.add_argument("day", type=date.fromisoformat)
    p.add_argument("--feed", choices=("iex", "sip"), default="iex")
    p.add_argument("--plan", type=Path, default=None)
    p.add_argument("--step", type=float, default=20.0)
    p = sub.add_parser("report")
    p.add_argument("day", nargs="?", type=date.fromisoformat, default=None)
    sub.add_parser("selftest")
    a = ap.parse_args(argv[1:])
    cfg = OptionsConfig()
    if a.cmd == "ingest-plan":
        return ingest_plan(a.file, cfg)
    if a.cmd == "replay":
        return replay_cmd(a.day, cfg, a.feed, a.plan, a.step)
    if a.cmd == "report":
        day = str(a.day or _now().astimezone(ET).date())
        with store.connect() as con:
            path = write_report(con, day, cfg)
        print(path.read_text(encoding="utf-8"))
        return 0
    if a.cmd == "selftest":
        return selftest()

    def _timeout(*_):
        raise SessionTimeout("options session exceeded its outer time bound")
    signal.signal(signal.SIGALRM, _timeout)
    now = _now()
    end = _at(now.astimezone(ET).date(), cfg.session_end)
    signal.alarm(max(int((end - now).total_seconds()) + SESSION_GRACE_S, SESSION_GRACE_S))
    try:
        return session(cfg)
    except SessionTimeout as e:
        log.error("%s", e)
        notify.send("期權軌道逾時", f"{e}。請手動確認期權部位已平倉。", notify.CRITICAL)
        return 1
    except Exception as e:
        log.exception("session failed")
        notify.send("期權軌道失敗", f"{type(e).__name__}: {e}。請手動確認期權部位已平倉。", notify.CRITICAL)
        return 1
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
