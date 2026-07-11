import pytest

from stockmoney.models.kelly import DEFAULT_CONSERVATIVE_COEFF, kelly_fraction, position_size


def test_kelly_fraction_known_value():
    # p_win=0.6, b=avg_win/avg_loss=2 -> (0.6*2 - 0.4)/2 = (1.2-0.4)/2 = 0.4
    assert kelly_fraction(0.6, avg_win=0.04, avg_loss=0.02) == pytest.approx(0.4)


def test_kelly_fraction_negative_edge_clipped_to_zero():
    # p_win=0.3, b=1 -> (0.3 - 0.7)/1 = -0.4 -> clipped to 0
    assert kelly_fraction(0.3, avg_win=0.01, avg_loss=0.01) == 0.0


def test_kelly_fraction_breakeven_is_zero():
    # p_win=0.5, b=1 -> (0.5 - 0.5)/1 = 0
    assert kelly_fraction(0.5, avg_win=0.01, avg_loss=0.01) == pytest.approx(0.0)


def test_kelly_fraction_zero_avg_loss_guarded():
    assert kelly_fraction(0.9, avg_win=0.05, avg_loss=0.0) == 0.0


def test_kelly_fraction_zero_avg_win_guarded():
    assert kelly_fraction(0.9, avg_win=0.0, avg_loss=0.01) == 0.0


def test_position_size_scales_by_conservative_coeff_and_confidence():
    kelly = kelly_fraction(0.6, avg_win=0.04, avg_loss=0.02)  # 0.4
    size = position_size(0.6, 0.04, 0.02, conservative_coeff=0.5, confidence_multiplier=0.8)
    assert size == pytest.approx(kelly * 0.5 * 0.8)


def test_position_size_default_coefficient_is_half_kelly():
    size = position_size(0.6, 0.04, 0.02)
    kelly = kelly_fraction(0.6, 0.04, 0.02)
    assert size == pytest.approx(kelly * DEFAULT_CONSERVATIVE_COEFF)


def test_position_size_zero_when_no_edge():
    assert position_size(0.2, avg_win=0.01, avg_loss=0.01) == 0.0
