"""Every JSON example handed to Muse must pass the real schema, or Muse
would learn a format the desk rejects."""
import re
from pathlib import Path

from pydantic import TypeAdapter

from stockmoney.desk.bridge.inbox import SUBJECT_RE
from stockmoney.desk.bridge.schema import Inbound

DOC = Path(__file__).resolve().parents[2] / "docs/desk/MUSE_ONBOARDING.md"


def test_every_json_example_is_valid():
    blocks = re.findall(r"```json\n(.*?)\n```", DOC.read_text(), re.S)
    assert len(blocks) >= 4
    types = set()
    for b in blocks:
        msg = TypeAdapter(Inbound).validate_json(b, strict=True)
        types.add(msg.type)
    assert {"SOCIAL_DAILY", "CALLS", "RESEARCH_REPLY", "ALERT"} <= types


def test_subject_example_matches_parser():
    assert SUBJECT_RE.match("[SMD/SOCIAL_DAILY/sd-20261012-0730] " + "a" * 48)
