"""Bridge poll cadence (2026-10-27). launchd starts ``desk_job.sh bridge`` every
60 seconds; this decides whether that start actually polls the mailbox.

- Weekdays 06:00-16:30 ET: every start polls (Grok intraday candidates and
  alerts must reach the desk within about a minute).
- Otherwise: poll only when the last poll is at least 15 minutes old. The last
  poll time is kept in bridge_ops (``bridge:last_poll``), so the gap survives
  restarts and sleep.

The desk lock (data/desk/run.lock) is taken without waiting: when another job
holds it this start is skipped silently and the next one tries again, so
bridge starts never queue behind premarket or postclose. A start that is not
due, or is skipped, prints nothing (desk_job.sh then writes no log lines).
"""
from __future__ import annotations

import logging
import signal
import sys
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
FAST_FROM = time(6, 0)
FAST_UNTIL = time(16, 30)
SLOW_EVERY = timedelta(minutes=15)
LAST_POLL_KEY = "bridge:last_poll"
IDLE = "Muse 新訊息 0 則"  # run.bridge with nothing new and nothing sent

log = logging.getLogger("desk.bridge_job")


def fast_window(now: datetime) -> bool:
    local = now.astimezone(ET)
    return local.weekday() < 5 and FAST_FROM <= local.time() < FAST_UNTIL


def last_poll(con) -> datetime | None:
    con.execute("CREATE TABLE IF NOT EXISTS bridge_ops (key TEXT PRIMARY KEY, at TEXT)")
    row = con.execute("SELECT at FROM bridge_ops WHERE key=?", (LAST_POLL_KEY,)).fetchone()
    if not row or not row[0]:
        return None
    try:
        t = datetime.fromisoformat(row[0])
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def mark_polled(con, now: datetime) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS bridge_ops (key TEXT PRIMARY KEY, at TEXT)")
    con.execute("INSERT INTO bridge_ops VALUES (?,?) ON CONFLICT(key) DO UPDATE SET at=excluded.at",
                (LAST_POLL_KEY, now.astimezone(timezone.utc).isoformat()))


def due(con, now: datetime) -> bool:
    if fast_window(now):
        return True
    last = last_poll(con)
    return last is None or now - last >= SLOW_EVERY or last > now  # a clock jump back never stalls it


def _page_once(now: datetime, text: str) -> None:
    """At 60-second starts a broken mailbox would page every minute: page once per ET day."""
    try:
        from stockmoney.desk import notify, store
        with store.connect() as con:
            cur = con.execute("INSERT OR IGNORE INTO bridge_ops VALUES (?,?)",
                              (f"bridge:fail:{now.astimezone(ET).date()}", now.isoformat()))
            first = cur.rowcount == 1
        if first:
            notify.send("Desk 任務失敗:bridge", text, notify.CRITICAL)
    except Exception:
        log.exception("bridge failure page failed")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from stockmoney.desk import store
    now = datetime.now(timezone.utc)
    with store.connect() as con:
        if not due(con, now):
            return 0
    from stockmoney.desk import run  # heavy import only when a poll is due
    lock = run._acquire_lock(run.LOCK_PATH, 0.0)
    if lock is None:
        return 0  # another desk job holds the lock; the next start retries

    def _timeout(*_):
        raise run.JobTimeout(f"bridge exceeded {run.JOB_TIMEOUT_S}s")
    signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(run.JOB_TIMEOUT_S)
    ok = False
    try:
        now = datetime.now(timezone.utc)
        with store.connect() as con:
            mark_polled(con, now)  # before the poll: a failing mailbox is not retried every minute off-hours
            con.commit()
            out = run.bridge(con, now)
        if out.strip() != IDLE:  # an empty poll leaves no log line
            print(out)
        ok = True
    except run.JobTimeout as e:
        signal.alarm(0)
        log.error("bridge timed out: %s", e)
        _page_once(now, f"bridge 逾時:{e}")
    except Exception as e:
        log.exception("bridge failed")
        _page_once(now, f"{type(e).__name__}: {e}")
    finally:
        signal.alarm(0)
        import fcntl
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
