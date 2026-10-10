"""The single all-in-one prompt for Muse: every JSON example must pass the
real schema (inbound examples against Inbound, desk-to-Muse ones against
Outbound), and every inbound message type must be covered."""
import re
from pathlib import Path

from pydantic import TypeAdapter

from stockmoney.desk.bridge.schema import Inbound, Outbound

DOC = Path(__file__).resolve().parents[2] / "docs/desk/MUSE_PROMPT.md"


def _blocks(tag: str) -> list[str]:
    return re.findall(rf"```{tag}\n(.*?)\n```", DOC.read_text(), re.S)


def test_inbound_examples_valid_and_cover_all_types():
    types = {TypeAdapter(Inbound).validate_json(b, strict=True).type for b in _blocks("json")}
    assert types == {"SOCIAL_DAILY", "CALLS", "ALERT", "RESEARCH_REPLY", "IDEA"}


def test_desk_examples_valid_and_cover_all_types():
    types = {TypeAdapter(Outbound).validate_json(b, strict=True).type for b in _blocks("json-desk")}
    assert types == {"WATCHLIST", "RESEARCH_REQUEST", "REPORT", "FEEDBACK"}


def test_prompt_keeps_placeholders_and_core_rules():
    text = DOC.read_text()
    assert all(ph in text for ph in ("<密鑰>", "<測試通道>", "<TO_DESK>", "<TO_MUSE>", "<通知管道>"))
    for needle in ("[SMD/<類型>/<訊息編號>]", "BTC/USD", "200KB", "通知管道", "不要交易", "America/New_York",
                   "不要發文", "抽樣計數", "沒通過就在這裡停止"):
        assert needle in text
    # WhatsApp may only appear in the correction table and cleanup step (as the thing being retracted)
    assert text.split("`````")[1].count("WhatsApp") == 3
