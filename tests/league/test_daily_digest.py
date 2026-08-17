"""Pure unit tests for the five-section plain-language daily digest (issue #7
P1, user stories 45-46). No DB, no LLM -- build_daily_digest is a pure
function of DigestInputs."""
from __future__ import annotations

from datetime import date

from stockmoney.league.daily_digest import MAX_CHARS, DigestInputs, build_daily_digest


def _inputs(**overrides) -> DigestInputs:
    base = dict(as_of=date(2026, 8, 17), market_note="盤面平淡,大盤小漲")
    base.update(overrides)
    return DigestInputs(**base)


def test_digest_has_exactly_five_labelled_sections_in_order():
    text = build_daily_digest(_inputs())
    labels = [line.split(":", 1)[0] for line in text.split("\n")]
    assert labels == ["今天盤是什麼樣", "系統做了什麼", "什麼變了", "捨棄了什麼", "需要你決定的事"]


def test_digest_reports_counts_in_actions_section():
    text = build_daily_digest(_inputs(predictions_recorded=12, confirmed=10, withdrawn=2, graded=5, still_pending=3))
    action_line = text.split("\n")[1]
    assert "12" in action_line and "10" in action_line and "2" in action_line
    assert "5" in action_line and "3" in action_line


def test_digest_decisions_needed_defaults_to_none_literal():
    """Story 46: an empty list must render the literal '無', never be
    silently omitted -- that's the whole point (it's the signal the system
    ran autonomously today)."""
    text = build_daily_digest(_inputs(decisions_needed=[]))
    assert "需要你決定的事:無" in text


def test_digest_decisions_needed_lists_items_when_present():
    text = build_daily_digest(_inputs(decisions_needed=["AVGO 選擇權停損已觸發,需要你確認是否平倉"]))
    assert "需要你決定的事:AVGO 選擇權停損已觸發,需要你確認是否平倉" in text


def test_digest_dropped_defaults_to_none_literal():
    text = build_daily_digest(_inputs(dropped=[]))
    assert "捨棄了什麼:無" in text


def test_digest_changes_defaults_to_no_obvious_change():
    text = build_daily_digest(_inputs(changes=[]))
    assert "什麼變了:無明顯變化" in text


def test_digest_never_exceeds_max_chars_even_with_a_long_market_note():
    long_note = "盤面震盪劇烈," * 60  # deliberately way over budget
    text = build_daily_digest(_inputs(market_note=long_note, decisions_needed=["a", "b", "c"]))
    assert len(text) <= MAX_CHARS
    # the fixed sections still all made it in, only the free-form note shrank
    assert "系統做了什麼" in text and "需要你決定的事" in text


def test_digest_is_pure_same_inputs_same_output():
    inputs = _inputs(predictions_recorded=3)
    assert build_daily_digest(inputs) == build_daily_digest(inputs)
