"""Operating the Muse bridge: correction feedback, delivery checks, scorecard.

Kept outside the bridge package on purpose: the bridge only parses and
flags; this module decides what to tell Muse and the owner about the
bridge's health. Nothing here can affect trading.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from stockmoney.desk import notify, store

ET = ZoneInfo("America/New_York")

SCHEMA = """
CREATE TABLE IF NOT EXISTS bridge_outbox (
  msg_id TEXT PRIMARY KEY, type TEXT, ticker TEXT, deadline TEXT, sent_at TEXT);
CREATE TABLE IF NOT EXISTS bridge_ops (key TEXT PRIMARY KEY, at TEXT);
"""


def ensure_schema(con) -> None:
    con.executescript(SCHEMA)
    from stockmoney.desk.bridge.inbox import ensure_schema as inbox_schema
    inbox_schema(con)


def _once(con, key: str) -> bool:
    """True the first time a key is seen (and records it)."""
    if con.execute("SELECT 1 FROM bridge_ops WHERE key=?", (key,)).fetchone():
        return False
    con.execute("INSERT INTO bridge_ops VALUES (?,?)", (key, store.now()))
    return True


def log_outbound(con, msg) -> None:
    con.execute(
        "INSERT OR IGNORE INTO bridge_outbox VALUES (?,?,?,?,?)",
        (msg.msg_id, msg.type, getattr(msg, "ticker", None),
         _iso(msg.deadline) if getattr(msg, "deadline", None) else None, store.now()))


# ------------------------------------------------------------------ feedback

FIXES = {
    "schema_invalid": "對照範本:欄位名稱與型別要完全一致,數字不要加引號,時間要用 ISO 8601 並含時區,不確定的欄位填 null,不要多加範本沒有的欄位。",
    "body_not_json": "內文要有一個完整的 JSON 物件(可以用 ```json 框起來),括號要成對。",
    "type_mismatch": "主旨裡的類型要和 JSON 的 \"type\" 欄位完全相同。",
    "msg_id_mismatch": "主旨裡的訊息編號要和 JSON 的 \"msg_id\" 欄位完全相同。",
    "duplicate_msg_id": "每封信的訊息編號都要不同,建議用「類型縮寫-日期-時間」,例如 sd-20261012-0730。",
    "oversized": "內容超過 200KB,請減少項目數量或拆成兩封。",
}


def pending_feedback(con) -> list[tuple[str, str, str]]:
    """(reject_key, about_msg_id, reason) for correctable rejects not yet answered.
    Only rejects that passed the sender and token checks are correctable, so an
    outsider can never make the desk send mail."""
    rows = con.execute("SELECT id, msg_id, reason FROM bridge_rejects ORDER BY id").fetchall()
    out = []
    for r in rows:
        code = (r[2] or "").split(" ")[0]
        if code in FIXES and not con.execute("SELECT 1 FROM bridge_ops WHERE key=?", (f"fb:{r[0]}",)).fetchone():
            out.append((f"fb:{r[0]}", r[1] or "unknown", r[2]))
    return out


def send_feedback(con, box, now: datetime) -> int:
    from stockmoney.desk.bridge import outbox
    from stockmoney.desk.bridge.schema import Feedback
    n = 0
    for key, about, reason in pending_feedback(con):
        code = reason.split(" ")[0]
        msg = Feedback(msg_id=outbox.new_msg_id("fb"), sent_at=now, about_msg_id=about[:64],
                       problem=f"訊息 {about} 被拒絕:{reason}"[:1000], how_to_fix=FIXES[code])
        outbox.send(box, msg)
        log_outbound(con, msg)
        _once(con, key)
        n += 1
    return n


# ------------------------------------------------------------------ delivery checks

@dataclass(frozen=True)
class Expectation:
    name: str
    type: str
    window_start: time  # ET; a message received after this time counts for today
    deadline: time      # ET


EXPECTATIONS = {
    "SOCIAL_AM": Expectation("早盤社群快照", "SOCIAL_DAILY", time(5, 0), time(8, 30)),
    "SOCIAL_PM": Expectation("盤後社群快照", "SOCIAL_DAILY", time(17, 0), time(21, 0)),
    "CALLS": Expectation("網紅喊單紀錄", "CALLS", time(17, 0), time(21, 30)),
}


def enabled_expectations(con) -> list[str]:
    """Which deliveries are expected; turned on stage by stage during onboarding
    (see docs/desk/MUSE_ONBOARDING.md) so the owner is not nagged early."""
    return json.loads(store.get_setting(con, "muse_expect", "[]"))


def _received(con, typ: str, start: datetime, end: datetime) -> bool:
    return con.execute(
        "SELECT 1 FROM bridge_messages WHERE type=? AND received_at>=? AND received_at<=?",
        (typ, _iso(start), _iso(end))).fetchone() is not None


def _iso(dt: datetime) -> str:
    from datetime import timezone
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def check_deliveries(con, now: datetime) -> list[str]:
    """Missed scheduled deliveries and overdue research replies (each reported once)."""
    local = now.astimezone(ET)
    day = local.date()
    misses = []
    for key in enabled_expectations(con):
        e = EXPECTATIONS.get(key)
        if e is None or local.time() < e.deadline:
            continue
        start = datetime.combine(day, e.window_start, ET)
        if not _received(con, e.type, start, now) and _once(con, f"miss:{key}:{day}"):
            misses.append(f"Muse 今天沒有準時送出「{e.name}」(期限 {e.deadline:%H:%M} ET)")
    for r in con.execute(
        "SELECT msg_id, ticker, deadline FROM bridge_outbox WHERE type='RESEARCH_REQUEST' AND deadline < ?",
        (_iso(now),),
    ).fetchall():
        answered = con.execute(
            "SELECT 1 FROM bridge_messages WHERE type='RESEARCH_REPLY' AND json_extract(parsed_json,'$.request_id')=?",
            (r[0],)).fetchone()
        if not answered and _once(con, f"late:{r[0]}"):
            misses.append(f"Muse 沒有在期限前回覆 {r[1]} 的進場前查核")
    return misses


def run_checks(con, now: datetime, box=None) -> str:
    ensure_schema(con)
    lines = []
    if box is not None:
        n = send_feedback(con, box, now)
        if n:
            lines.append(f"已寄 {n} 則格式修正給 Muse")
    for m in check_deliveries(con, now):
        notify.send("Muse 漏交", m, notify.WARN)
        lines.append(m)
    return "\n".join(lines)


# ------------------------------------------------------------------ scorecard

def scorecard(con, now: datetime, days: int = 7) -> dict:
    """On-time delivery rate per enabled expectation, accept/reject counts,
    research reply rate. Used by the console and the onboarding acceptance."""
    ensure_schema(con)
    local = now.astimezone(ET)
    since = now - timedelta(days=days)
    out: dict = {"window_days": days, "deliveries": {}}
    for key in enabled_expectations(con) or list(EXPECTATIONS):
        e = EXPECTATIONS[key]
        hit = total = 0
        for i in range(days):
            d = local.date() - timedelta(days=i)
            if datetime.combine(d, e.deadline, ET) > local:
                continue
            total += 1
            start = datetime.combine(d, e.window_start, ET)
            end = datetime.combine(d, e.deadline, ET)
            hit += _received(con, e.type, start, end)
        out["deliveries"][key] = {"name": e.name, "on_time": hit, "due": total}
    out["accepted"] = con.execute("SELECT COUNT(*) FROM bridge_messages WHERE received_at>=?", (_iso(since),)).fetchone()[0]
    out["rejected"] = con.execute("SELECT COUNT(*) FROM bridge_rejects WHERE received_at>=?", (_iso(since),)).fetchone()[0]
    req = con.execute("SELECT msg_id FROM bridge_outbox WHERE type='RESEARCH_REQUEST' AND sent_at>=?", (_iso(since),)).fetchall()
    answered = sum(
        1 for r in req if con.execute(
            "SELECT 1 FROM bridge_messages WHERE type='RESEARCH_REPLY' AND json_extract(parsed_json,'$.request_id')=?",
            (r[0],)).fetchone())
    out["research"] = {"requested": len(req), "answered": answered}
    return out
