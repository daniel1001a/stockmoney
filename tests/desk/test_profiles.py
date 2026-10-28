import pytest

from stockmoney.desk import profiles


def test_default_is_standard():
    p = profiles.active({})
    assert (p.risk_per_trade, p.max_positions, p.max_per_sector) == (0.0075, 8, 2)


def test_aggressive_on_paper():
    p = profiles.active({"DESK_PROFILE": "paper_aggressive", "ALPACA_ENV": "paper"})
    assert p.risk_per_trade == 0.015 and p.limits().max_position_pct == 0.25


def test_aggressive_refused_on_live():
    with pytest.raises(ValueError, match="paper-only"):
        profiles.active({"DESK_PROFILE": "paper_aggressive", "ALPACA_ENV": "live"})


def test_unknown_profile():
    with pytest.raises(ValueError):
        profiles.active({"DESK_PROFILE": "yolo"})
