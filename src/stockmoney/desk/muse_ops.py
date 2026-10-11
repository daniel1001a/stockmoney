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
    # the message's own send time (the orchestrator's clock), so rolling-window
    # counts below agree with the ``now`` they are given
    sent = getattr(msg, "sent_at", None)
    con.execute(
        "INSERT OR IGNORE INTO bridge_outbox VALUES (?,?,?,?,?)",
        (msg.msg_id, msg.type, getattr(msg, "ticker", None),
         _iso(msg.deadline) if getattr(msg, "deadline", None) else None,
         _iso(sent) if sent else store.now()))


# ------------------------------------------------------------------ feedback

FIXES = {
    "schema_invalid": "對照範本:欄位名稱與型別要完全一致,數字不要加引號,時間要用 ISO 8601 並含時區,不確定的欄位填 null,不要多加範本沒有的欄位。",
    "body_not_json": "內文要有一個完整的 JSON 物件(可以用 ```json 框起來),括號要成對。",
    "type_mismatch": "主旨裡的類型要和 JSON 的 \"type\" 欄位完全相同。",
    "msg_id_mismatch": "主旨裡的訊息編號要和 JSON 的 \"msg_id\" 欄位完全相同。",
    "duplicate_msg_id": "每封信的訊息編號都要不同,建議用「類型縮寫-日期-時間」,例如 sd-20261012-0730。",
    "oversized": "內容超過 200KB,請減少項目數量或拆成兩封。",
}


JSON_INVALID_FIX = (
    "JSON 無法解析。最常見的原因:字串值裡出現換行(例如「AI 概念股」的空格被換成換行)。"
    "請讓每個字串值保持在同一行,拉丁字母與中文之間不要放空格(寫成「AI概念股」),"
    "也可以把整個 JSON 壓成單行。protocol 欄位可以省略;要寫就必須是字串 \"1\",不能是整數 1。"
)


def fields_hint(reason: str) -> str:
    """For a schema rejection that names a message type (loc 'ALERT.ticker'),
    list that type's required and optional top-level fields from the real
    model, so the sender can fix a missing or misplaced field without guessing."""
    import re
    from stockmoney.desk.bridge.schema import Alert, Calls, GrokDaily, Idea, ResearchReply, SocialDaily
    models = {"SOCIAL_DAILY": SocialDaily, "CALLS": Calls, "ALERT": Alert,
              "RESEARCH_REPLY": ResearchReply, "IDEA": Idea, "GROK_DAILY": GrokDaily}
    m = re.match(r"schema_invalid\s+([A-Z_]+)\.", reason)
    model = models.get(m.group(1)) if m else None
    if model is None:
        return ""
    required = [n for n, f in model.model_fields.items() if f.is_required() or n == "type"]
    optional = [n for n, f in model.model_fields.items() if n not in required]
    return (f"{m.group(1)} 的頂層必填欄位:{', '.join(required)};選填:{', '.join(optional) or '無'}。"
            "多出範例沒有的欄位(extra_forbidden)與缺少必填欄位(missing)都會被拒絕;"
            "別的類型的欄位不能搬過來用。")


# Rules a GROK_DAILY can break that the bare error code ("value_error",
# "string_pattern_mismatch") does not explain.
GROK_RULES = (
    "GROK_DAILY 規則:msg_id 以 grok- 開頭;所有時間帶美東偏移(夏令 -04:00、冬令 -05:00),不能用 Z;"
    "collected_at 在 sent_at 之前 6 小時內;比特幣寫 BTC/USD,股票寫 BRK-B;網址只能 https 且不超過 250 字元;"
    "帳號寫成 @handle;calls 的 platform 一律 \"x\";checkpoints 的 date 寫 YYYY-MM-DD。"
)


def fix_for(reason: str) -> str:
    """The correction text for a stored reject reason."""
    if "json_invalid" in reason:
        return JSON_INVALID_FIX
    base = FIXES[reason.split(" ")[0]]
    hint = fields_hint(reason)
    text = f"{hint} {base}" if hint else base
    return f"{text} {GROK_RULES}" if "GROK_DAILY" in reason else text


# Hard ceiling on correction mail so that no loop, whatever its cause, can
# flood Muse's mailbox: 10 per rolling 24 hours, the owner told once.
FEEDBACK_CAP = 10
FEEDBACK_WINDOW = timedelta(hours=24)


def pending_feedback(con) -> list[tuple[str, str, str]]:
    """(reject_key, about_msg_id, reason) for correctable rejects not yet answered.
    Only rejects that passed the sender and token checks are correctable, so an
    outsider can never make the desk send mail. Rejects of mail that quotes the
    desk's own mail (reply/forward prefix, outbound-only type) are never
    correctable: answering them is what made the feedback loop."""
    from stockmoney.desk.bridge.inbox import is_echo_subject
    rows = con.execute("SELECT id, msg_id, reason, subject FROM bridge_rejects ORDER BY id").fetchall()
    out = []
    for r in rows:
        code = (r[2] or "").split(" ")[0]
        if code not in FIXES or is_echo_subject(r[3]):
            continue
        if not con.execute("SELECT 1 FROM bridge_ops WHERE key=?", (f"fb:{r[0]}",)).fetchone():
            out.append((f"fb:{r[0]}", r[1] or "unknown", r[2]))
    return out


def feedback_sent_since(con, since: datetime) -> list[str]:
    """sent_at (newest first) of FEEDBACK mails sent after ``since``."""
    return [r[0] for r in con.execute(
        "SELECT sent_at FROM bridge_outbox WHERE type='FEEDBACK' AND sent_at > ? ORDER BY sent_at DESC",
        (_iso(since),)).fetchall()]


def send_feedback(con, box, now: datetime) -> int:
    from stockmoney.desk.bridge import outbox
    from stockmoney.desk.bridge.schema import Feedback
    pending = pending_feedback(con)
    budget = FEEDBACK_CAP - len(feedback_sent_since(con, now - FEEDBACK_WINDOW))
    n = 0
    for key, about, reason in pending[: max(budget, 0)]:
        # Grok sends from the same address, so its corrections land in the mailbox
        # Muse reads; the tag tells Muse to leave them alone (MUSE_PROMPT.md)
        tag = "【給 Grok】" if about.lower().startswith("grok-") else ""
        msg = Feedback(msg_id=outbox.new_msg_id("fb"), sent_at=now, about_msg_id=about[:64],
                       problem=f"{tag}訊息 {about} 被拒絕:{reason}"[:1000], how_to_fix=fix_for(reason))
        outbox.send(box, msg)
        log_outbound(con, msg)
        _once(con, key)
        n += 1
    if len(pending) > n:  # left unsent: they go out as the window frees up
        recent = feedback_sent_since(con, now - FEEDBACK_WINDOW)
        # one notice per capped episode: keyed by the newest mail inside the cap
        marker = recent[FEEDBACK_CAP - 1] if len(recent) >= FEEDBACK_CAP else _iso(now)
        if _once(con, f"fbcap:{marker}"):
            notify.send(
                "Muse 格式修正信已達上限",
                f"24 小時內已寄 {FEEDBACK_CAP} 則格式修正給 Muse,還有 {len(pending) - n} 則待寄,暫停寄出。"
                "請檢查橋樑是否在互相回信。",
                notify.WARN)
    return n


# ------------------------------------------------------------------ delivery checks

@dataclass(frozen=True)
class Expectation:
    name: str
    type: str
    window_start: time  # ET; a message received after this time counts for today
    deadline: time      # ET
    who: str = "Muse"
    trading_days_only: bool = False      # weekends / NYSE holidays: still stored, never "missed"
    match_collected_date: bool = False   # only a message collected that ET day counts (no stale resends)
    collected_before: time | None = None  # ET; with match_collected_date: a later run (the evening one) never counts


EXPECTATIONS = {
    "SOCIAL_AM": Expectation("早盤社群快照", "SOCIAL_DAILY", time(5, 0), time(8, 30)),
    "SOCIAL_PM": Expectation("盤後社群快照", "SOCIAL_DAILY", time(17, 0), time(21, 0)),
    "CALLS": Expectation("網紅喊單紀錄", "CALLS", time(17, 0), time(21, 30)),
    # Grok also sends an evening digest (20:00); it is stored but never scored
    # or checked, and collected_before keeps it from passing as the morning one.
    "GROK_AM": Expectation("Grok 盤前日報", "GROK_DAILY", time(4, 0), time(8, 30), who="Grok",
                           trading_days_only=True, match_collected_date=True, collected_before=time(12, 0)),
}

# Grok's checks live under their own setting: muse_check.py "stage N" rewrites
# muse_expect wholesale and must not switch Grok's off.
GROK_EXPECT_KEY = "grok_expect"


def enabled_expectations(con) -> list[str]:
    """Which deliveries are expected; turned on stage by stage during onboarding
    (see docs/desk/MUSE_ONBOARDING.md) so the owner is not nagged early. Grok's
    are switched separately (muse_check.py grok on|off)."""
    keys = json.loads(store.get_setting(con, "muse_expect", "[]"))
    keys += [k for k in json.loads(store.get_setting(con, GROK_EXPECT_KEY, "[]")) if k not in keys]
    return keys


def _due(e: Expectation, day) -> bool:
    from stockmoney.desk.calendar import is_trading_day
    return not e.trading_days_only or is_trading_day(day)


def _received(con, typ: str, start: datetime, end: datetime, collected_on=None,
              collected_before: time | None = None) -> bool:
    sql = "SELECT 1 FROM bridge_messages WHERE type=? AND received_at>=? AND received_at<=?"
    args: tuple = (typ, _iso(start), _iso(end))
    if collected_on is not None:
        # collected_at keeps its ET offset in parsed_json ("2026-10-12T07:05:00-04:00"):
        # chars 1-10 are the ET date, chars 12-16 the ET wall-clock HH:MM
        sql += " AND substr(json_extract(parsed_json,'$.collected_at'),1,10)=?"
        args += (collected_on.isoformat(),)
        if collected_before is not None:
            sql += " AND substr(json_extract(parsed_json,'$.collected_at'),12,5)<?"
            args += (f"{collected_before:%H:%M}",)
    return con.execute(sql, args).fetchone() is not None


def _delivered(con, e: Expectation, day, start: datetime, end: datetime) -> bool:
    if e.match_collected_date:
        return _received(con, e.type, start, end, day, e.collected_before)
    return _received(con, e.type, start, end)


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
        if e is None or local.time() < e.deadline or not _due(e, day):
            continue
        start = datetime.combine(day, e.window_start, ET)
        got = _delivered(con, e, day, start, now)
        if not got and _once(con, f"miss:{key}:{day}"):
            misses.append(f"{e.who} 今天沒有準時送出「{e.name}」(期限 {e.deadline:%H:%M} ET)")
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


# ------------------------------------------------------------------ Grok scope

GROK_EXTRA_SCOPE = ("QQQ", "TQQQ", "BTC/USD")
GROK_MAX_OFF_LIST = 5


def grok_scope() -> frozenset[str]:
    from stockmoney.desk.universe import ALL_SYMBOLS
    return frozenset(ALL_SYMBOLS) | frozenset(GROK_EXTRA_SCOPE)


def grok_off_list(parsed: dict) -> list[str]:
    """Distinct tickers in a stored GROK_DAILY that are outside the Grok scope."""
    tickers = {x["ticker"] for k in ("events", "calls", "attention") for x in parsed.get(k, [])}
    tickers |= {i["ticker"] for c in parsed.get("chains", []) for i in c.get("impacts", [])}
    return sorted(tickers - grok_scope())


def check_grok_scope(con, since: datetime) -> list[str]:
    """More than GROK_MAX_OFF_LIST off-list tickers in one GROK_DAILY: tell the
    owner once per message. Never rejects: the message is already stored."""
    out = []
    for r in con.execute("SELECT msg_id, parsed_json FROM bridge_messages WHERE type='GROK_DAILY' AND received_at>=?",
                         (_iso(since),)).fetchall():
        try:
            extra = grok_off_list(json.loads(r[1]))
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        if len(extra) > GROK_MAX_OFF_LIST and _once(con, f"grokscope:{r[0]}"):
            out.append(f"Grok 日報 {r[0]} 有 {len(extra)} 檔清單外標的(上限 {GROK_MAX_OFF_LIST}):{', '.join(extra[:12])}")
    return out


def run_checks(con, now: datetime, box=None) -> str:
    ensure_schema(con)
    lines = []
    if box is not None:
        n = send_feedback(con, box, now)
        if n:
            lines.append(f"已寄 {n} 則格式修正給 Muse")
    for m in check_deliveries(con, now):
        notify.send("Grok 漏交" if m.startswith("Grok ") else "Muse 漏交", m, notify.WARN)
        lines.append(m)
    for m in check_grok_scope(con, now - timedelta(days=2)):
        notify.send("Grok 範圍", m, notify.INFO)
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
            if datetime.combine(d, e.deadline, ET) > local or not _due(e, d):
                continue
            total += 1
            start = datetime.combine(d, e.window_start, ET)
            end = datetime.combine(d, e.deadline, ET)
            hit += _delivered(con, e, d, start, end)
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
