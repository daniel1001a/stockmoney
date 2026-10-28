"""Every JSON example handed to Grok must pass the real schema, or Grok would
learn a format the desk rejects."""
import re
from pathlib import Path

from pydantic import TypeAdapter

from stockmoney.desk.bridge.inbox import SUBJECT_RE
from stockmoney.desk.bridge.schema import Inbound
from stockmoney.desk.muse_ops import grok_off_list

DOC = Path(__file__).resolve().parents[2] / "docs/desk/GROK_PROMPT.md"


def test_every_json_example_is_a_valid_grok_daily():
    blocks = re.findall(r"```json\n(.*?)\n```", DOC.read_text(), re.S)
    assert blocks
    for b in blocks:
        msg = TypeAdapter(Inbound).validate_json(b, strict=True)
        assert msg.type == "GROK_DAILY"
        assert grok_off_list(msg.model_dump(mode="json")) == []


def test_subject_example_matches_parser_and_rules_are_present():
    text = DOC.read_text()
    m = re.search(r"`(\[SMD/GROK_DAILY/grok-[^\]]+\]) <密鑰>`", text)
    assert m and SUBJECT_RE.match(m.group(1) + " " + "a" * 48)
    for needle in ("BTC/USD", "BRK-B", "-04:00", "-05:00", "collected_at", "【給 Grok】", "200KB", "GROK_ADDRESS", "super-pika@mail.grokbot.com"):
        assert needle in text
    assert "run_at" not in text and "BRK.B, " not in text and "TQQQ, BTC," not in text
    for needle in ("dnial1001a@gmail.com", "mckang1001@gmail.com", "grok-YYYYMMDD-HHMM", "20:00", "07:30", "08:10",
                   "text/plain", "America/New_York", "SPY, QQQ, TQQQ, BTC/USD", "送 []", '"x"', "null"):
        assert needle in text, needle
    assert "Tasks" not in text and '"lang"' not in text


def test_doc_uses_the_ticker_forms_the_schema_and_universe_use():
    from stockmoney.desk.bridge.schema import GrokAttention
    from stockmoney.desk.universe import ALL_SYMBOLS
    assert "BRK-B" in ALL_SYMBOLS
    for t in ("BRK-B", "BTC/USD", "QQQ", "TQQQ"):
        GrokAttention(ticker=t, trend="rising", why="x")
