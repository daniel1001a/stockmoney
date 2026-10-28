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
CREATE TABLE IF NOT EXISTS bridge_pings (
  msg_id TEXT PRIMARY KEY, channel TEXT NOT NULL, nonce TEXT NOT NULL, sent_at TEXT NOT NULL);
"""


def ensure_schema(con) -> None:
    con.executescript(SCHEMA)
    # bridge_outbox.channel (2026-10-27): "muse" or "grok"; NULL on older rows means muse
    if "channel" not in {r[1] for r in con.execute("PRAGMA table_info(bridge_outbox)")}:
        con.execute("ALTER TABLE bridge_outbox ADD COLUMN channel TEXT")
    from stockmoney.desk.bridge.inbox import ensure_schema as inbox_schema
    inbox_schema(con)


def _once(con, key: str) -> bool:
    """True the first time a key is seen (and records it)."""
    if con.execute("SELECT 1 FROM bridge_ops WHERE key=?", (key,)).fetchone():
        return False
    con.execute("INSERT INTO bridge_ops VALUES (?,?)", (key, store.now()))
    return True


def log_outbound(con, msg, channel: str = "muse") -> None:
    # the message's own send time (the orchestrator's clock), so rolling-window
    # counts below agree with the ``now`` they are given
    sent = getattr(msg, "sent_at", None)
    con.execute(
        "INSERT OR IGNORE INTO bridge_outbox (msg_id, type, ticker, deadline, sent_at, channel) VALUES (?,?,?,?,?,?)",
        (msg.msg_id, msg.type, getattr(msg, "ticker", None),
         _iso(msg.deadline) if getattr(msg, "deadline", None) else None,
         _iso(sent) if sent else store.now(), channel))


# ------------------------------------------------------------------ feedback

FIXES = {
    "schema_invalid": "對照範本:欄位名稱與型別要完全一致,數字不要加引號,時間要用 ISO 8601 並含時區,不確定的欄位填 null,不要多加範本沒有的欄位。",
    "body_not_json": "內文要有一個完整的 JSON 物件(可以用 ```json 框起來),括號要成對。",
    "type_mismatch": "主旨裡的類型要和 JSON 的 \"type\" 欄位完全相同。",
    "msg_id_mismatch": "主旨裡的訊息編號要和 JSON 的 \"msg_id\" 欄位完全相同。",
    "duplicate_msg_id": "這個訊息編號已收過另一份內容。內容完全相同的重送會被系統安靜略過;內容不同請換新編號,例如結尾加 -r2(grok-20261021-0730-r2)。新信建議用「類型縮寫-日期-時間」,例如 sd-20261012-0730。",
    "plan_id_reused": "這個 plan_id 已被內容不同的劇本用過。內容不同的重送一定要換新的 plan_id,例如 gp-20261015-0830b;內容完全相同的重送會被系統安靜略過。",
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
    from stockmoney.desk.bridge.schema import Alert, Calls, GrokDaily, Idea, Pong, ResearchReply, SocialDaily
    from stockmoney.desk.options.plan import OptionsPlan
    models = {"OPTIONS_PLAN": OptionsPlan, "SOCIAL_DAILY": SocialDaily, "CALLS": Calls, "ALERT": Alert,
              "RESEARCH_REPLY": ResearchReply, "IDEA": Idea, "GROK_DAILY": GrokDaily, "PONG": Pong}
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


PLAN_RULES = (
    "OPTIONS_PLAN 規則:主旨 [SMD/OPTIONS_PLAN/<plan_id>] 的 plan_id 要等於 JSON 的 plan_id(不要寫 msg_id、sent_at);"
    "plan_id 不能以 grok- 開頭;整份單行 JSON、不超過 64000 位元組;所有時間帶美東偏移(-04:00 / -05:00),不能用 Z;"
    "trigger 的 earliest/latest 在 09:35 到 11:30;bias 只能是 long 或 short;plans 最多 12 個;不能有多餘欄位。"
    "細節以 GROK_SETUP.md 的 OPTIONS_PLAN 格式為準。"
)


# Field names Grok's OPTIONS_PLAN mails (10-15 to 10-21) used that the model does
# not have, each with where the information belongs instead. Matched against the
# reject's loc so only the mistakes this mail made are named.
PLAN_FIELD_FIXES = (
    (r"OPTIONS_PLAN\.daily_ref:extra_forbidden", "頂層不能有 daily_ref,要引用 GROK_DAILY 就寫在每個 plans 項目的 daily_ref。"),
    (r"OPTIONS_PLAN\.key_events:extra_forbidden",
     'key_events 要放進 market_context:"market_context":{"summary":..,"risk_tone":..,"key_events":[{"time":..,"what":..}]}。'),
    (r"OPTIONS_PLAN\.avoid:missing", "avoid 必填,沒有就寫 []。"),
    (r"plans\.\d+\.ticker:extra_forbidden", "plans 項目的 ticker 要改名為 underlying。"),
    (r"plans\.\d+\.rvol:extra_forbidden", "rvol 不是欄位,最低相對成交量寫在 min_rvol。"),
    (r"plans\.\d+\.timing:extra_forbidden", "timing 不是欄位,進場時間窗寫在 trigger 的 earliest/latest。"),
    (r"plans\.\d+\.gap:extra_forbidden", "gap 不是欄位,缺口幅度寫進 thesis 文字。"),
    (r"plans\.\d+\.premarket_price:extra_forbidden", "premarket_price 不是欄位,盤前價寫進 thesis 文字。"),
)


def plan_item_required() -> str:
    from stockmoney.desk.options.plan import PlanItem
    names = "/".join(n for n, f in PlanItem.model_fields.items() if f.is_required())
    return f"每個劇本必填 {names};bias 是評分用的假設。看不準方向就不寫,plans 可以是 []。"


def plan_field_fixes(reason: str) -> str:
    """Where each misplaced or renamed field in an OPTIONS_PLAN reject belongs,
    for only the mistakes this reject names."""
    import re
    out = [fix for pat, fix in PLAN_FIELD_FIXES if re.search(pat, reason)]
    # item fields that already have their own row above
    named = {pat.split("\\.")[-1].split(":")[0] for pat, _ in PLAN_FIELD_FIXES if pat.startswith("plans\\.")}
    other = sorted({f for f in re.findall(r"plans\.\d+\.(\w+):extra_forbidden", reason) if f not in named})
    if other:
        out.append(f"{', '.join(f[:30] for f in other[:3])} 不是劇本欄位,寫進 thesis。")
    if re.search(r"plans\.\d+\.", reason):
        # the reason lists at most 5 errors, so a missing bias can hide behind them
        out.append(plan_item_required())
    return " ".join(out) + " " if out else ""


def fix_for(reason: str) -> str:
    """The correction text for a stored reject reason."""
    if "json_invalid" in reason:
        return JSON_INVALID_FIX
    base = FIXES[reason.split(" ")[0]]
    hint = fields_hint(reason)
    text = f"{hint} {base}" if hint else base
    if "OPTIONS_PLAN" in reason:
        fixes = plan_field_fixes(reason)
        text = f"{fixes}{text} {PLAN_RULES}"
        if len(text) > 1000:  # how_to_fix limit: the per-field fixes matter more than the field list
            text = f"{fixes}{base} {PLAN_RULES}"[:1000]
    return f"{text} {GROK_RULES}" if "GROK_DAILY" in reason else text


# Hard ceiling on correction mail so that no loop, whatever its cause, can
# flood a mailbox: 10 per rolling 24 hours PER CHANNEL (Muse, Grok), the owner
# told once per channel. Drop notices (accepted with fields dropped) count too.
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


def feedback_sent_since(con, since: datetime, channel: str | None = None) -> list[str]:
    """sent_at (newest first) of FEEDBACK mails sent after ``since``; with
    ``channel``, only that channel's (a NULL channel is a pre-2026-10-27 Muse mail)."""
    sql = "SELECT sent_at FROM bridge_outbox WHERE type='FEEDBACK' AND sent_at > ?"
    args: tuple = (_iso(since),)
    if channel is not None:
        sql += " AND COALESCE(channel,'muse')=?"
        args += (channel,)
    return [r[0] for r in con.execute(sql + " ORDER BY sent_at DESC", args).fetchall()]


DROP_FIX = ("訊息已接受,但上面列出的欄位或項目不合格式,已被丟棄、沒有存入。"
            "對照範本修正:多餘欄位不要寫,網址只能 https,清單項目每一筆都要完整。下一封照範本寫就不會再收到這封通知。")


def pending_drop_notices(con) -> list[tuple[str, str, str, str]]:
    """(key, msg_id, summary, sender) for accepted messages that were pruned
    (bridge_messages.dropped_json) and not yet answered. A pruned Re:/Fwd: mail
    is stored with feedback=false and never answered."""
    from stockmoney.desk.bridge.inbox import drop_summary
    if "dropped_json" not in {r[1] for r in con.execute("PRAGMA table_info(bridge_messages)")}:
        return []
    out = []
    for r in con.execute("SELECT msg_id, sender, dropped_json FROM bridge_messages WHERE dropped_json IS NOT NULL "
                         "ORDER BY received_at, msg_id").fetchall():
        try:
            d = json.loads(r[2])
        except ValueError:
            continue
        if not isinstance(d, dict) or not d.get("feedback") or not d.get("dropped"):
            continue
        key = f"fbd:{r[0]}"
        if not con.execute("SELECT 1 FROM bridge_ops WHERE key=?", (key,)).fetchone():
            out.append((key, r[0], f"{len(d['dropped'])} 個欄位/項目被丟棄:{drop_summary(d['dropped'])}", r[1] or ""))
    return out


def _channel_for(sender: str | None, config) -> str:
    """The channel a reply goes back on: Grok's own address gets Grok's channel."""
    s = (sender or "").strip().lower()
    return "grok" if config.grok_enabled and s and s == config.grok_address.strip().lower() else "muse"


def _reject_sender(con, key: str) -> str:
    r = con.execute("SELECT sender FROM bridge_rejects WHERE id=?", (key.removeprefix("fb:"),)).fetchone()
    return (r[0] if r else "") or ""


def _is_plan_reject(con, key: str) -> bool:
    """True when the reject came from an OPTIONS_PLAN subject (Grok's, sent from
    the same address as Muse's mail; its plan_id has no grok- prefix)."""
    r = con.execute("SELECT subject FROM bridge_rejects WHERE id=?", (key.removeprefix("fb:"),)).fetchone()
    return bool(r and r[0] and r[0].lstrip().find("[SMD/OPTIONS_PLAN/") >= 0)


def send_feedback(con, box, now: datetime, config=None) -> int:
    """Answer correctable rejects and pruned (accepted-with-drops) messages on
    the channel they came from: Grok's address gets Grok's channel and token,
    everything else Muse's. Capped per channel."""
    from stockmoney.desk.bridge import outbox
    from stockmoney.desk.bridge.mailbox import BridgeConfig
    from stockmoney.desk.bridge.schema import Feedback
    rejects, drops = pending_feedback(con), pending_drop_notices(con)
    if not rejects and not drops:
        return 0
    config = config or BridgeConfig.from_env()
    items = []  # (key, channel, Feedback fields)
    for key, about, reason in rejects:
        channel = _channel_for(_reject_sender(con, key), config)
        # Grok's legacy mail comes from Muse's address, so its corrections land in
        # the mailbox Muse reads; the tag tells Muse to leave them alone (MUSE_PROMPT.md)
        legacy_grok = about.lower().startswith("grok-") or _is_plan_reject(con, key)
        tag = "【給 Grok】" if channel == "muse" and legacy_grok else ""
        items.append((key, channel, dict(about_msg_id=about[:64], problem=f"{tag}訊息 {about} 被拒絕:{reason}"[:1000],
                                         how_to_fix=fix_for(reason))))
    for key, about, summary, sender in drops:
        channel = _channel_for(sender, config)
        tag = "【給 Grok】" if channel == "muse" and about.lower().startswith("grok-") else ""
        items.append((key, channel, dict(about_msg_id=about[:64],
                                         problem=f"{tag}訊息 {about} 已接受(非致命):{summary}"[:1000],
                                         how_to_fix=DROP_FIX)))
    n = 0
    left = {"muse": 0, "grok": 0}
    budget = {c: FEEDBACK_CAP - len(feedback_sent_since(con, now - FEEDBACK_WINDOW, c)) for c in left}
    for key, channel, fields in items:
        if budget[channel] <= 0:
            left[channel] += 1
            continue
        msg = Feedback(msg_id=outbox.new_msg_id("fb"), sent_at=now, **fields)
        outbox.send(box, msg, config, channel=channel)
        log_outbound(con, msg, channel)
        _once(con, key)
        budget[channel] -= 1
        n += 1
    for channel, waiting in left.items():
        if not waiting:  # left unsent: they go out as the window frees up
            continue
        recent = feedback_sent_since(con, now - FEEDBACK_WINDOW, channel)
        # one notice per capped episode: keyed by the newest mail inside the cap
        marker = recent[FEEDBACK_CAP - 1] if len(recent) >= FEEDBACK_CAP else _iso(now)
        who = "Muse" if channel == "muse" else "Grok"
        if _once(con, f"fbcap:{marker}" if channel == "muse" else f"fbcap:grok:{marker}"):
            notify.send(
                f"{who} 格式修正信已達上限",
                f"24 小時內已寄 {FEEDBACK_CAP} 則格式修正給 {who},還有 {waiting} 則待寄,暫停寄出。"
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
            # "no mail" is not "nothing happened": an empty daily is a delivery
            # (events: []) and never lands here
            note = ";沒有收到信(不是空白日報),不代表今天沒有風險" if e.who == "Grok" else ""
            misses.append(f"{e.who} 今天沒有準時送出「{e.name}」(期限 {e.deadline:%H:%M} ET){note}")
    for r in con.execute(
        "SELECT msg_id, ticker, deadline, COALESCE(channel,'muse') FROM bridge_outbox "
        "WHERE type='RESEARCH_REQUEST' AND deadline < ?",
        (_iso(now),),
    ).fetchall():
        answered = con.execute(
            "SELECT 1 FROM bridge_messages WHERE type='RESEARCH_REPLY' AND json_extract(parsed_json,'$.request_id')=?",
            (r[0],)).fetchone()
        if not answered and _once(con, f"late:{r[0]}"):
            if r[3] == "grok":
                misses.append(f"Grok 沒有在期限前回覆 {r[1]} 的盤後原因查詢")
            else:
                misses.append(f"Muse 沒有在期限前回覆 {r[1]} 的進場前查核")
    return misses


def grok_today(con, now: datetime) -> dict:
    """Today's morning GROK_DAILY status, for the console and reports.

    status: not_due (weekend / NYSE holiday), pending (before the deadline,
    nothing yet), missed (deadline passed with no accepted mail: no mail is not
    "no risk"), empty (a valid daily with no events), delivered. Uses the same
    window and collected-date rule as check_deliveries; works whether or not
    the miss notification is switched on (``check_on`` says which)."""
    e = EXPECTATIONS["GROK_AM"]
    local = now.astimezone(ET)
    day = local.date()
    start = datetime.combine(day, e.window_start, ET)
    rejected = con.execute("SELECT COUNT(*) FROM bridge_rejects WHERE received_at>=? AND received_at<=? "
                           "AND subject LIKE '%SMD/GROK_DAILY/%'", (_iso(start), _iso(now))).fetchone()[0]
    out = {"day": day.isoformat(), "deadline": f"{e.deadline:%H:%M}", "msg_id": None, "events": None,
           "rejected": rejected, "check_on": "GROK_AM" in enabled_expectations(con)}
    rows = []
    if local.time() >= e.window_start:
        rows = con.execute(
            "SELECT msg_id, parsed_json FROM bridge_messages WHERE type='GROK_DAILY' AND received_at>=? "
            "AND received_at<=? AND substr(json_extract(parsed_json,'$.collected_at'),1,10)=? "
            "AND substr(json_extract(parsed_json,'$.collected_at'),12,5)<? ORDER BY received_at",
            (_iso(start), _iso(now), day.isoformat(), f"{e.collected_before:%H:%M}")).fetchall()
    if rows:
        events = 0
        for r in rows:
            try:
                events += len(json.loads(r[1]).get("events") or [])
            except (ValueError, TypeError, AttributeError):
                continue
        out.update(msg_id=rows[-1][0], events=events, status="delivered" if events else "empty")
    elif not _due(e, day):
        out["status"] = "not_due"
    elif local.time() < e.deadline:
        out["status"] = "pending"
    else:
        out["status"] = "missed"
    return out


IN_PLAY_CUTOFF = time(9, 20)  # ET; the same cutoff as OPTIONS_PLAN (later: shadow only)


def grok_in_play(con, day) -> list[dict]:
    """The ``in_play`` candidates of ``day``'s morning GROK_DAILY mail: only
    grok_verified rows received that ET day before 09:20 ET (by the desk's own
    clock, received_at). grok_legacy and Muse rows never count. Each item is
    the stored dict plus ``msg_id`` and ``received_at``; a later message comes
    after an earlier one. A candidate is a proposal only: rules and the risk
    layer decide every order."""
    start = datetime.combine(day, time(0, 0), ET)
    cutoff = datetime.combine(day, IN_PLAY_CUTOFF, ET)
    out = []
    for msg_id, received_at, parsed in con.execute(
            "SELECT msg_id, received_at, parsed_json FROM bridge_messages WHERE type='GROK_DAILY' "
            "AND source='grok_verified' AND received_at>=? AND received_at<? ORDER BY received_at, msg_id",
            (_iso(start), _iso(cutoff))).fetchall():
        try:
            items = json.loads(parsed).get("in_play") or []
        except (ValueError, TypeError, AttributeError):
            continue
        for it in items:
            if isinstance(it, dict):
                out.append(dict(it, msg_id=msg_id, received_at=received_at))
    return out


GROK_TODAY_LABEL = {
    "not_due": "今天休市,不檢查早報",
    "pending": "早報尚未收到(期限 {deadline} ET 前)",
    "missed": "漏交:{deadline} ET 前沒有收到信,不代表今天沒有風險",
    "empty": "已收到空白日報({msg_id}):Pika 回報今天沒有事件",
    "delivered": "已收到早報({msg_id}):{events} 則事件",
}


def grok_today_text(st: dict) -> str:
    text = GROK_TODAY_LABEL[st["status"]].format(**st)
    if st["rejected"] and st["status"] in ("pending", "missed"):
        text += f";今天有 {st['rejected']} 封 GROK_DAILY 被拒,見被拒絕的訊息"
    return text


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
            lines.append(f"已寄 {n} 則格式修正信")
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


# ---------------------------------------------------------------- PING / PONG handshake (2026-10-27)

PING_ASK = ("Handshake test. Reply with a PONG that echoes this nonce and states what you can do "
            "(email read/send, HTTP GET/POST, scheduling and the times you run).")


def send_ping(con, box, channel: str, now: datetime, config=None):
    """Send a PING on ``channel`` ("grok" or "muse") and record its nonce."""
    import secrets

    from stockmoney.desk.bridge import outbox
    from stockmoney.desk.bridge.schema import Ping
    ensure_schema(con)
    msg = Ping(msg_id=outbox.new_msg_id(f"ping-{channel}"), sent_at=now,
               nonce=secrets.token_hex(8), ask=PING_ASK)
    outbox.send(box, msg, config, channel=channel)
    log_outbound(con, msg, channel)
    con.execute("INSERT INTO bridge_pings VALUES (?,?,?,?)", (msg.msg_id, channel, msg.nonce, _iso(now)))
    return msg


def ping_results(con, limit: int = 20) -> list[dict]:
    """Each PING (newest first) with the PONG that echoed its nonce from the same
    channel, if any: round trip in seconds and the capabilities it declared."""
    ensure_schema(con)
    from stockmoney.desk.bridge import inbox
    inbox.ensure_schema(con)
    pongs: dict[tuple[str, str], tuple] = {}
    for received, parsed, source in con.execute(
            "SELECT received_at, parsed_json, source FROM bridge_messages WHERE type='PONG' ORDER BY received_at"):
        try:
            p = json.loads(parsed)
        except (TypeError, ValueError):
            continue
        ch = "grok" if str(source or "").startswith("grok") else "muse"
        pongs.setdefault((ch, p.get("nonce", "")), (received, p, source))
    out = []
    for msg_id, ch, nonce, sent in con.execute(
            "SELECT msg_id, channel, nonce, sent_at FROM bridge_pings ORDER BY sent_at DESC LIMIT ?", (limit,)):
        hit = pongs.get((ch, nonce))
        row = dict(msg_id=msg_id, channel=ch, nonce=nonce, sent_at=sent, answered=hit is not None)
        if hit:
            received, p, source = hit
            row.update(received_at=received, source=source,
                       rtt_s=round((datetime.fromisoformat(received) - datetime.fromisoformat(sent)).total_seconds()),
                       capabilities=p.get("capabilities"), notes=p.get("notes", ""))
        out.append(row)
    return out
