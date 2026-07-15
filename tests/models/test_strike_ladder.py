import pytest

from stockmoney.models.strike_ladder import is_on_ladder, snap_strike, strike_increment


# --- strike_increment: tier boundaries -------------------------------------

def test_increment_sub_25_is_half_dollar():
    assert strike_increment(12.34) == 0.5


def test_increment_25_to_100_is_one_dollar():
    assert strike_increment(30.0) == 1.0
    assert strike_increment(25.0) == 1.0  # lower bound inclusive


def test_increment_100_to_250_is_two_fifty():
    assert strike_increment(247.3) == 2.5   # SOXX example from the bug report
    assert strike_increment(234.6) == 2.5   # TSLA example from the bug report
    assert strike_increment(100.0) == 2.5   # lower bound inclusive


def test_increment_250_to_500_is_five_dollar():
    assert strike_increment(250.0) == 5.0
    assert strike_increment(400.0) == 5.0


def test_increment_above_500_is_ten_dollar():
    assert strike_increment(996.7) == 10.0  # NFLX example from the bug report
    assert strike_increment(500.0) == 10.0


def test_increment_rejects_non_positive_and_nan():
    with pytest.raises(ValueError):
        strike_increment(0.0)
    with pytest.raises(ValueError):
        strike_increment(-5.0)
    with pytest.raises(ValueError):
        strike_increment(float("nan"))


# --- snap_strike: never fractional garbage, always on-ladder ---------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        (996.7139482, 1000.0),     # NFLX-like: >500 tier, $10 increments
        (234.62193, 235.0),        # TSLA-like: 100-250 tier, $2.5 increments
        (247.313, 247.5),          # SOXX-like: 100-250 tier, $2.5 increments
        (30.42, 30.0),             # a $30 name: 25-100 tier, $1 increments
        (12.34, 12.5),             # a $12 name: <25 tier, $0.5 increments
    ],
)
def test_snap_strike_examples_from_the_bug_report(raw, expected):
    assert snap_strike(raw) == pytest.approx(expected)


def test_snapped_strike_is_never_fractional_garbage():
    for raw in [996.7139482, 234.62193, 247.313, 583.827, 1059.39, 712.44, 87.5329, 59.9253]:
        snapped = snap_strike(raw)
        inc = strike_increment(snapped)
        # snapped / inc must be a whole number (no fractional-cent garbage)
        ratio = snapped / inc
        assert ratio == pytest.approx(round(ratio), abs=1e-9), (raw, snapped, inc)


def test_snap_strike_is_idempotent():
    for raw in [996.7139482, 234.62193, 247.313, 30.42, 12.34, 1.1, 4999.9]:
        once = snap_strike(raw)
        twice = snap_strike(once)
        assert once == twice


def test_is_on_ladder_true_for_snapped_values():
    for raw in [996.7139482, 234.62193, 247.313, 30.42, 12.34]:
        assert is_on_ladder(snap_strike(raw))


def test_is_on_ladder_false_for_the_reported_impossible_strikes():
    for bad in [996.7, 234.6, 247.3]:
        assert not is_on_ladder(bad)


def test_is_on_ladder_false_for_non_positive():
    assert not is_on_ladder(0.0)
    assert not is_on_ladder(-10.0)
