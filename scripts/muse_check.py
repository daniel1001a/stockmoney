"""Muse bridge setup check and onboarding acceptance.

    .venv/bin/python scripts/muse_check.py mailbox      # read-only: config + IMAP login + unread count
    .venv/bin/python scripts/muse_check.py status       # scorecard for the last 7 days + recent rejects
    .venv/bin/python scripts/muse_check.py stage 2      # turn on delivery checks for stage 2 (see MUSE_ONBOARDING.md)
    .venv/bin/python scripts/muse_check.py drill        # stage 3: send Muse a test research request (ticker DRILL)
    .venv/bin/python scripts/muse_check.py grok on      # Grok GROK_DAILY lateness check on (off: grok off)

Load .env first (scripts/desk_job.sh does this for scheduled jobs):
    set -a; source .env; set +a
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

from stockmoney.desk import muse_ops, store

STAGE_EXPECT = {1: [], 2: ["SOCIAL_AM", "SOCIAL_PM", "CALLS"], 3: ["SOCIAL_AM", "SOCIAL_PM", "CALLS"]}


def cmd_mailbox() -> int:
    from stockmoney.desk.bridge.mailbox import BridgeConfig, ImapSmtpMailbox
    try:
        cfg = BridgeConfig.from_env()
    except Exception as e:
        print(f"設定錯誤:{e}")
        return 2
    print(f"Desk 信箱:{cfg.desk_address};接受來自:{cfg.muse_address};密鑰長度 {len(cfg.shared_token)}")
    try:
        with ImapSmtpMailbox(cfg) as box:
            mails = box.fetch_unseen(1)  # max_bytes=1: headers only, nothing is marked read
        print(f"IMAP 登入成功,未讀信件 {len(mails)} 封(未標記已讀)。")
        return 0
    except Exception as e:
        print(f"IMAP 連線失敗:{type(e).__name__}: {e}")
        return 1


def cmd_status() -> int:
    now = datetime.now(timezone.utc)
    with store.connect() as con:
        muse_ops.ensure_schema(con)
        card = muse_ops.scorecard(con, now)
        print(json.dumps(card, ensure_ascii=False, indent=2))
        rows = con.execute("SELECT received_at, msg_id, reason FROM bridge_rejects ORDER BY id DESC LIMIT 10").fetchall()
        if rows:
            print("\n最近被拒絕:")
            for r in rows:
                print(f"  {r[0]}  {r[1] or '-'}  {r[2]}")
        last = con.execute("SELECT received_at, type, msg_id FROM bridge_messages ORDER BY received_at DESC LIMIT 10").fetchall()
        print("\n最近收到:" if last else "\n尚未收到任何 Muse 訊息。")
        for r in last:
            print(f"  {r[0]}  {r[1]}  {r[2]}")
    return 0


def cmd_stage(n: int) -> int:
    with store.connect() as con:
        store.set_setting(con, "muse_expect", json.dumps(STAGE_EXPECT[n]))
        store.set_setting(con, "muse_stage", str(n))
    print(f"已進入第 {n} 階段,漏交檢查:{STAGE_EXPECT[n] or '無'}")
    return 0


def cmd_grok(state: str) -> int:
    """Grok's delivery check has its own setting, so "stage N" never touches it."""
    keys = ["GROK_AM"] if state == "on" else []
    with store.connect() as con:
        store.set_setting(con, muse_ops.GROK_EXPECT_KEY, json.dumps(keys))
    print(f"Grok 漏交檢查:{'開啟(交易日 08:30 ET 前要收到 GROK_DAILY)' if keys else '關閉'}")
    return 0


def cmd_drill() -> int:
    from stockmoney.desk.bridge import outbox
    from stockmoney.desk.bridge.mailbox import BridgeConfig, ImapSmtpMailbox
    from stockmoney.desk.bridge.schema import ResearchRequest
    now = datetime.now(timezone.utc)
    msg = ResearchRequest(msg_id=outbox.new_msg_id("drill"), sent_at=now, ticker="DRILL",
                          deadline=now + timedelta(hours=2),
                          questions=["這是演練。請回覆 RESEARCH_REPLY,risk_flags 留空,summary 寫「演練收到」。"])
    with store.connect() as con, ImapSmtpMailbox(BridgeConfig.from_env()) as box:
        muse_ops.ensure_schema(con)
        outbox.send(box, msg)
        muse_ops.log_outbound(con, msg)
    print(f"已寄出演練請求 {msg.msg_id},期限 2 小時。用 status 檢查是否收到回覆。")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "mailbox":
        return cmd_mailbox()
    if cmd == "status":
        return cmd_status()
    if cmd == "stage" and len(argv) == 3 and int(argv[2]) in STAGE_EXPECT:
        return cmd_stage(int(argv[2]))
    if cmd == "drill":
        return cmd_drill()
    if cmd == "grok" and len(argv) == 3 and argv[2] in ("on", "off"):
        return cmd_grok(argv[2])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
