"""Daily plain-language digest (issue #7 P1, CONTEXT.md, user stories 45-46):
a single, fixed five-section brief the user can read without watching the
dashboard -- 今天盤是什麼樣 / 系統做了什麼 / 什麼變了 / 捨棄了什麼 / 需要你決定的事,
capped around 300 characters total.

Deliberately a PURE function: takes an already-assembled DigestInputs, touches
no database, calls no LLM (CLAUDE.md section 13/17: no local thinking model,
and the OpenClaw LLM's role stops at reading review output, never writing the
call-facing text) -- template-rendered plain language only, so the same
inputs always produce the same digest and it is trivially unit-testable.
`orchestration.run_decision_point`'s post_close branch is what assembles the
real DigestInputs from the day's DB state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

MAX_CHARS = 300


@dataclass
class DigestInputs:
    as_of: date
    market_note: str                     # 今天盤是什麼樣 -- short plain-language regime/session note
    predictions_recorded: int = 0
    confirmed: int = 0
    withdrawn: int = 0
    graded: int = 0
    still_pending: int = 0
    changes: list[str] = field(default_factory=list)      # 什麼變了
    dropped: list[str] = field(default_factory=list)       # 捨棄了什麼 (skips/withdrawals/rejections)
    decisions_needed: list[str] = field(default_factory=list)  # 需要你決定的事; empty -> "無"


def _join_or(items: list[str], empty: str) -> str:
    return "、".join(items) if items else empty


def build_daily_digest(inputs: DigestInputs) -> str:
    """Five fixed sections, one per line, each 標籤: 內容. Truncates only the
    free-form market_note if the total would exceed MAX_CHARS -- the other
    four sections are short, bounded, mechanically-generated summaries that
    should never need truncation in practice."""
    actions = (
        f"今天記錄 {inputs.predictions_recorded} 筆新判斷,"
        f"確認 {inputs.confirmed} 筆、撤回 {inputs.withdrawn} 筆;"
        f"評分到期 {inputs.graded} 筆,尚有 {inputs.still_pending} 筆待到期。"
    )
    changes_text = _join_or(inputs.changes, "無明顯變化")
    dropped_text = _join_or(inputs.dropped, "無")
    decisions_text = _join_or(inputs.decisions_needed, "無")

    sections = [
        f"今天盤是什麼樣:{inputs.market_note}",
        f"系統做了什麼:{actions}",
        f"什麼變了:{changes_text}",
        f"捨棄了什麼:{dropped_text}",
        f"需要你決定的事:{decisions_text}",
    ]
    text = "\n".join(sections)

    if len(text) > MAX_CHARS:
        # Shrink the one genuinely free-form section (market_note) first --
        # everything else is a short, bounded, mechanically-generated line.
        overflow = len(text) - MAX_CHARS
        keep = max(0, len(inputs.market_note) - overflow - 1)  # -1 for the ellipsis char
        trimmed_note = inputs.market_note[:keep] + "…" if keep < len(inputs.market_note) else inputs.market_note
        sections[0] = f"今天盤是什麼樣:{trimmed_note}"
        text = "\n".join(sections)

    return text[:MAX_CHARS]
