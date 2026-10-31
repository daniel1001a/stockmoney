"""Send one GROK_NOTICE file to Grok over its own channel (owner-run only).

    python scripts/desk_grok_notice.py --file docs/desk/notices/<id>.json --dry-run
    python scripts/desk_grok_notice.py --file docs/desk/notices/<id>.json

--dry-run validates the file and prints the exact subject and body that would
be mailed; nothing is sent or recorded. A real send needs the bridge
environment with GROK_ADDRESS / GROK_SHARED_TOKEN set, and refuses a
notice_id that was already sent. Grok's NOTICE_ACK is matched by the inbox;
with no ack after 24 hours the owner is pushed once.
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from stockmoney.desk import grok_notice, store


def main(argv=None, *, mailbox=None, config=None, db: Path = store.DEFAULT_PATH, now=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    notice = grok_notice.load(args.file, now or datetime.now(timezone.utc))
    if args.dry_run:
        subject, body = grok_notice.render(notice)
        print(f"Subject: {subject}\n\n{body}")
        return 0
    from stockmoney.desk.bridge import mailbox as mb
    config = config or mb.BridgeConfig.from_env()
    if not config.grok_enabled:
        print("GROK_ADDRESS / GROK_SHARED_TOKEN are not configured; nothing sent.", file=sys.stderr)
        return 2
    with store.connect(db) as con:
        if grok_notice.sent_at(con, notice.notice_id):
            print(f"{notice.notice_id} was already sent; write a new notice_id.", file=sys.stderr)
            return 1
        if mailbox is not None:
            grok_notice.send(con, mailbox, notice, config)
        else:
            with mb.ImapSmtpMailbox(config) as box:
                grok_notice.send(con, box, notice, config)
    print(f"sent {notice.notice_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
