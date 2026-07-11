from datetime import date, timedelta

import pytest

from stockmoney.models.options_risk import (
    GREEN, RED, YELLOW,
    MarketSnapshot,
    OptionPosition,
    assess_position,
)

ENTRY_DATE = date(2026, 1, 1)


def _long_call(**overrides):
    defaults = dict(
        position_id="p1", symbol="SOXL", option_right="call", side="long",
        entry_date=ENTRY_DATE, entry_underlying_price=100.0, entry_premium=10.0,
        entry_iv=0.5,
    )
    defaults.update(overrides)
    return OptionPosition(**defaults)


def _long_put(**overrides):
    defaults = dict(
        position_id="p2", symbol="SOXL", option_right="put", side="long",
        entry_date=ENTRY_DATE, entry_underlying_price=100.0, entry_premium=10.0,
        entry_iv=0.5,
    )
    defaults.update(overrides)
    return OptionPosition(**defaults)


def _short_put(**overrides):
    defaults = dict(
        position_id="p3", symbol="SOXL", option_right="put", side="short",
        entry_date=ENTRY_DATE, entry_underlying_price=100.0, entry_premium=10.0,
        entry_iv=0.5,
    )
    defaults.update(overrides)
    return OptionPosition(**defaults)


def test_invalid_option_right_rejected():
    with pytest.raises(ValueError):
        OptionPosition(
            position_id="x", symbol="A", option_right="banana", side="long",
            entry_date=ENTRY_DATE, entry_underlying_price=1, entry_premium=1,
        )


def test_invalid_side_rejected():
    with pytest.raises(ValueError):
        OptionPosition(
            position_id="x", symbol="A", option_right="call", side="sideways",
            entry_date=ENTRY_DATE, entry_underlying_price=1, entry_premium=1,
        )


def test_green_when_nothing_moved():
    position = _long_call()
    snapshot = MarketSnapshot(
        as_of_date=ENTRY_DATE + timedelta(days=10),
        underlying_price=100.0, current_premium=10.0,
    )
    result = assess_position(position, snapshot)
    assert result.light == GREEN
    assert result.triggers == []


def test_price_stop_fires_red_on_large_adverse_move_for_long_call():
    position = _long_call()
    # 30 days held, sigma_price = 100 * 0.5 * sqrt(30/365) ~= 14.3; a $40 drop is ~2.8 std devs
    snapshot = MarketSnapshot(as_of_date=ENTRY_DATE + timedelta(days=30), underlying_price=60.0)
    result = assess_position(position, snapshot)
    kinds = {t.kind: t.light for t in result.triggers}
    assert kinds["price_stop"] == RED
    assert result.light == RED


def test_price_stop_direction_is_mirrored_for_long_put():
    # For a long put, a *rising* underlying is the adverse direction.
    position = _long_put()
    snapshot = MarketSnapshot(as_of_date=ENTRY_DATE + timedelta(days=30), underlying_price=140.0)
    result = assess_position(position, snapshot)
    kinds = {t.kind: t.light for t in result.triggers}
    assert kinds["price_stop"] == RED

    # The same move in the other direction should NOT trigger a put's price stop.
    snapshot_favorable = MarketSnapshot(as_of_date=ENTRY_DATE + timedelta(days=30), underlying_price=60.0)
    result_favorable = assess_position(position, snapshot_favorable)
    assert "price_stop" not in {t.kind for t in result_favorable.triggers}


def test_price_stop_skipped_without_entry_iv():
    position = _long_call(entry_iv=None)
    snapshot = MarketSnapshot(as_of_date=ENTRY_DATE + timedelta(days=30), underlying_price=1.0)
    result = assess_position(position, snapshot)
    assert "price_stop" not in {t.kind for t in result.triggers}
    assert any("entry_iv missing" in n for n in result.notes)


def test_premium_stop_fires_red_at_minus_50_pct():
    position = _long_call()
    snapshot = MarketSnapshot(as_of_date=ENTRY_DATE, underlying_price=100.0, current_premium=5.0)
    result = assess_position(position, snapshot)
    kinds = {t.kind: t.light for t in result.triggers}
    assert kinds["premium_stop"] == RED


def test_premium_stop_yellow_approaching():
    position = _long_call()
    # -42% is within the 80%-of-threshold yellow band (-40% to -50%)
    snapshot = MarketSnapshot(as_of_date=ENTRY_DATE, underlying_price=100.0, current_premium=5.8)
    result = assess_position(position, snapshot)
    kinds = {t.kind: t.light for t in result.triggers}
    assert kinds["premium_stop"] == YELLOW


def test_take_profit_fires_red_at_plus_100_pct():
    position = _long_call()
    snapshot = MarketSnapshot(as_of_date=ENTRY_DATE, underlying_price=100.0, current_premium=20.0)
    result = assess_position(position, snapshot)
    kinds = {t.kind: t.light for t in result.triggers}
    assert kinds["take_profit"] == RED


def test_dynamic_ev_trigger_fires_red_when_negative():
    position = _long_call()
    snapshot = MarketSnapshot(
        as_of_date=ENTRY_DATE, underlying_price=100.0, current_premium=10.0,
        ev_of_continuing=-0.01,
    )
    result = assess_position(position, snapshot)
    kinds = {t.kind: t.light for t in result.triggers}
    assert kinds["dynamic_ev_take_profit"] == RED


def test_dynamic_ev_trigger_skipped_when_not_provided():
    position = _long_call()
    snapshot = MarketSnapshot(as_of_date=ENTRY_DATE, underlying_price=100.0, current_premium=10.0)
    result = assess_position(position, snapshot)
    assert "dynamic_ev_take_profit" not in {t.kind for t in result.triggers}
    assert any("ev_of_continuing missing" in n for n in result.notes)


def test_short_position_ignores_long_only_triggers():
    position = _short_put()
    # Premium *dropped* a lot -- would be a red premium_stop for a long, but
    # this is a short/seller position so premium_stop must not apply at all.
    snapshot = MarketSnapshot(as_of_date=ENTRY_DATE, underlying_price=100.0, current_premium=1.0)
    result = assess_position(position, snapshot)
    kinds = {t.kind for t in result.triggers}
    assert "premium_stop" not in kinds
    assert "take_profit" not in kinds
    assert "dynamic_ev_take_profit" not in kinds


def test_seller_early_close_yellow_in_window():
    position = _short_put()
    # captured 60% of premium as profit
    snapshot = MarketSnapshot(as_of_date=ENTRY_DATE, underlying_price=100.0, current_premium=4.0)
    result = assess_position(position, snapshot)
    kinds = {t.kind: t.light for t in result.triggers}
    assert kinds["seller_early_close"] == YELLOW


def test_seller_early_close_red_past_max_capture():
    position = _short_put()
    # captured 85% of premium
    snapshot = MarketSnapshot(as_of_date=ENTRY_DATE, underlying_price=100.0, current_premium=1.5)
    result = assess_position(position, snapshot)
    kinds = {t.kind: t.light for t in result.triggers}
    assert kinds["seller_early_close"] == RED


def test_seller_early_close_green_below_window():
    position = _short_put()
    # captured only 20% of premium
    snapshot = MarketSnapshot(as_of_date=ENTRY_DATE, underlying_price=100.0, current_premium=8.0)
    result = assess_position(position, snapshot)
    assert "seller_early_close" not in {t.kind for t in result.triggers}
    assert result.light == GREEN


def test_regime_invalidation_fires_red_on_mismatch():
    position = _long_call(regime_at_entry=0)
    snapshot = MarketSnapshot(
        as_of_date=ENTRY_DATE, underlying_price=100.0, current_premium=10.0, current_regime=2,
    )
    result = assess_position(position, snapshot)
    kinds = {t.kind: t.light for t in result.triggers}
    assert kinds["regime_invalidation"] == RED


def test_regime_invalidation_quiet_on_match():
    position = _long_call(regime_at_entry=1)
    snapshot = MarketSnapshot(
        as_of_date=ENTRY_DATE, underlying_price=100.0, current_premium=10.0, current_regime=1,
    )
    result = assess_position(position, snapshot)
    assert "regime_invalidation" not in {t.kind for t in result.triggers}


def test_regime_invalidation_skipped_when_data_missing():
    position = _long_call()  # regime_at_entry defaults to None
    snapshot = MarketSnapshot(as_of_date=ENTRY_DATE, underlying_price=100.0, current_premium=10.0)
    result = assess_position(position, snapshot)
    assert "regime_invalidation" not in {t.kind for t in result.triggers}
    assert any("regime_invalidation not evaluated" in n for n in result.notes)


def test_overall_light_is_worst_of_all_triggers():
    position = _long_call()
    snapshot = MarketSnapshot(
        as_of_date=ENTRY_DATE, underlying_price=100.0, current_premium=5.8,  # yellow premium_stop
        ev_of_continuing=-0.01,  # red dynamic ev
    )
    result = assess_position(position, snapshot)
    assert result.light == RED
