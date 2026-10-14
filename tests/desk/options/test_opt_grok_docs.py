"""The Grok-facing documents must match the plan validator (the validator is
authoritative): every example JSON in GROK_OPTIONS_PROMPT.md part 2 parses,
and the format rules in GROK_SETUP.md name the same fields, windows and
limits as plan.py."""
import json
import re
from pathlib import Path

import pytest

from stockmoney.desk.options import plan as oplan

DOCS = Path(__file__).resolve().parents[3] / "docs/desk"
PROMPT = (DOCS / "GROK_OPTIONS_PROMPT.md").read_text(encoding="utf-8")
SETUP = (DOCS / "GROK_SETUP.md").read_text(encoding="utf-8")


def part2() -> str:
    return PROMPT[PROMPT.index("## 第二部分"):PROMPT.index("## 第三部分")]


def examples() -> list[str]:
    return [l for l in part2().splitlines() if l.startswith('{"protocol"')]


def test_part2_has_the_three_examples_and_all_validate():
    ex = examples()
    assert len(ex) == 3
    parsed = [oplan.parse(t) for t in ex]
    assert parsed[0].plans and not parsed[0].plans[0].directional          # a triggered item
    assert parsed[1].plans == []                                           # an empty plan
    assert parsed[2].plans and all(i.directional for i in parsed[2].plans)  # a directional prior
    assert len({p.plan_id for p in parsed}) == 3
    for t in ex:  # single-line JSON objects, as the prompt demands
        assert isinstance(json.loads(t), dict)


def _fields(model) -> tuple[set[str], set[str]]:
    req = {n for n, f in model.model_fields.items() if f.is_required()}
    return req, set(model.model_fields) - req


def _line(text: str, start: str) -> str:
    return next(l for l in text.splitlines() if l.strip().startswith(start))


@pytest.mark.parametrize("doc", ["setup", "prompt"])
def test_item_required_and_optional_fields_match(doc):
    req, opt = _fields(oplan.PlanItem)
    assert req == {"underlying", "bias", "playbook", "thesis", "catalyst", "confidence", "evidence_urls"}
    if doc == "setup":
        must, may = _line(SETUP, "必填:"), _line(SETUP, "選填")
    else:
        p2 = part2()
        must = p2[p2.index("【每個劇本必填】"):p2.index("【選填")]
        may = p2[p2.index("【選填"):p2.index("【沒有 trigger")]
    for f in req:
        assert re.search(rf"\b{f}\b", must), (doc, "required", f)
        assert not re.search(rf"\b{f}\b", may), (doc, "listed as optional", f)
    for f in opt:
        assert re.search(rf"\b{f}\b", may), (doc, "optional", f)


def test_top_level_fields_and_windows_match():
    req, opt = _fields(oplan.OptionsPlan)
    top = _line(SETUP, "頂層必填")
    assert not opt and all(re.search(rf"\b{f}\b", top) for f in req)
    assert f"{oplan.KEY_EVENT_START} 到 {oplan.KEY_EVENT_END} ET" in SETUP
    assert f"{oplan.KEY_EVENT_START} 到 {oplan.KEY_EVENT_END} ET" in part2()
    assert f"{oplan.WINDOW_START} 到 {oplan.WINDOW_END}" in SETUP
    assert f"{oplan.WINDOW_START} 到 {oplan.WINDOW_END}" in part2()


def _max_len(model, field) -> int:
    for m in model.model_fields[field].metadata:
        if hasattr(m, "max_length") and m.max_length is not None:
            return m.max_length
    raise AssertionError(field)


def _str_max(model, field) -> int:
    import typing
    ann = model.model_fields[field].annotation
    args = typing.get_args(ann) or (ann,)
    for a in args:
        for m in getattr(a, "__metadata__", ()) + tuple(model.model_fields[field].metadata):
            if getattr(m, "max_length", None):
                return m.max_length
    raise AssertionError(field)


LIMITS = [  # (model, field, how the docs write it)
    (oplan.OptionsPlan, "plans", "plans(<={n}"),
    (oplan.OptionsPlan, "avoid", "avoid(<={n}"),
    (oplan.MarketContext, "key_events", "key_events(<={n}"),
]
TEXT_LIMITS = [
    (oplan.PlanItem, "thesis", "thesis(1 到 {n} 字"), (oplan.PlanItem, "catalyst", "catalyst(<={n} 字"),
    (oplan.MarketContext, "summary", "summary(<={n} 字"), (oplan.OptionsPlan, "notes", "notes(<={n} 字"),
    (oplan.AvoidItem, "why", "why(<={n} 字"), (oplan.KeyEvent, "what", "what(<={n} 字"),
    (oplan.PlanItem, "daily_ref", "daily_ref(<={n} 字"),
]


@pytest.mark.parametrize("model,field,fmt", LIMITS)
def test_list_limits_in_setup(model, field, fmt):
    assert fmt.format(n=_max_len(model, field)) in SETUP


@pytest.mark.parametrize("model,field,fmt", TEXT_LIMITS)
def test_text_limits_in_setup(model, field, fmt):
    assert fmt.format(n=_str_max(model, field)) in SETUP, field
