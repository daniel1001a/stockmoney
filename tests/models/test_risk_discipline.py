import pytest

from stockmoney.models.risk_discipline import (
    RiskLimits,
    daily_loss_breached,
    loss_streak_cooldown,
    recommended_position_fraction,
)


def test_position_fraction_is_fractional_kelly_capped():
    # A strong edge would size big under Kelly; the hard cap must bind.
    frac = recommended_position_fraction(0.7, 0.05, 0.02)
    assert frac == pytest.approx(RiskLimits().max_position_frac)


def test_position_fraction_zero_when_no_edge():
    # p_win 0.4 with symmetric payoff -> negative Kelly -> clipped to 0 (no trade).
    assert recommended_position_fraction(0.4, 0.02, 0.02) == 0.0


def test_position_fraction_scales_below_cap_for_thin_edge():
    # A small positive edge sizes to a small positive fraction, under the cap.
    frac = recommended_position_fraction(0.55, 0.02, 0.02)
    assert 0.0 < frac < RiskLimits().max_position_frac


def test_confidence_multiplier_scales_but_still_capped():
    base = recommended_position_fraction(0.6, 0.03, 0.02, confidence_multiplier=1.0)
    half = recommended_position_fraction(0.6, 0.03, 0.02, confidence_multiplier=0.5)
    assert half < base or base == RiskLimits().max_position_frac
    # A zero/negative confidence never yields a negative size.
    assert recommended_position_fraction(0.6, 0.03, 0.02, confidence_multiplier=-1.0) == 0.0


def test_daily_loss_breaker_trips_at_threshold_only():
    limits = RiskLimits(daily_loss_limit=0.04)
    equity = 25_000.0
    assert daily_loss_breached(-0.05 * equity, equity, limits=limits) is True
    assert daily_loss_breached(-0.04 * equity, equity, limits=limits) is True  # at threshold
    assert daily_loss_breached(-0.03 * equity, equity, limits=limits) is False
    assert daily_loss_breached(+0.10 * equity, equity, limits=limits) is False  # a gain never trips


def test_daily_loss_breaker_safe_on_zero_equity():
    assert daily_loss_breached(-100.0, 0.0) is False


def test_loss_streak_cooldown_counts_trailing_losses_only():
    limits = RiskLimits(loss_streak_cooldown=3)
    assert loss_streak_cooldown(["win", "loss", "loss", "loss"], limits=limits) is True
    # A recent win breaks the streak even if earlier trades lost.
    assert loss_streak_cooldown(["loss", "loss", "loss", "win"], limits=limits) is False
    assert loss_streak_cooldown(["loss", "loss"], limits=limits) is False
