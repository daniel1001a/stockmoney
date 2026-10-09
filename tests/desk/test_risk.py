"""Risk engine tests. Defaults: capital_budget 50,000; all prices below are 100
for equities unless stated; BTC 50,000; ETH 2,000."""
import pytest
from desk_helpers import buy, sell

from stockmoney.desk import store
from stockmoney.desk.execution.risk import Limits, RiskEngine
from stockmoney.desk.execution.types import AssetClass, BrokerPosition, OrderType


def decide(engine, broker, intent, ref=100.0, orders=0, **kw):
    return engine.check(intent, broker.account(), ref, orders, **kw)


# ------------------------------------------------------------ basic allow
def test_plain_buy_allowed_unchanged(make_engine, broker):
    d = decide(make_engine(), broker, buy(qty=10))
    assert d.allow and d.intent == buy(qty=10) and d.reason == "ok"


def test_invalid_intents_blocked(make_engine, broker):
    e = make_engine()
    assert not decide(e, broker, buy(qty=10, notional=1000)).allow
    assert not decide(e, broker, buy()).allow
    assert not decide(e, broker, buy(qty=-1)).allow
    assert not decide(e, broker, buy(qty=10), ref=0).allow
    assert not decide(e, broker, buy(qty=10), ref=float("nan")).allow


# ------------------------------------------------------------ kill switch
def test_kill_file_blocks_buy(make_engine, broker, halt_file):
    halt_file.touch()
    d = decide(make_engine(), broker, buy(qty=10))
    assert not d.allow and d.intent is None and "kill switch" in d.reason


def test_kill_file_absent_allows_buy(make_engine, broker):
    assert decide(make_engine(), broker, buy(qty=10)).allow


def test_kill_setting_blocks_buy(make_engine, broker, con):
    store.set_setting(con, "halt_new_entries", "1")
    d = decide(make_engine(), broker, buy(qty=10))
    assert not d.allow and "halt_new_entries" in d.reason


def test_kill_setting_off_allows_buy(make_engine, broker, con):
    store.set_setting(con, "halt_new_entries", "0")
    assert decide(make_engine(), broker, buy(qty=10)).allow


def test_kill_switch_but_sell_allowed(make_engine, broker, hold, halt_file, con):
    hold("AAPL", "breakout", 50)
    halt_file.touch()
    store.set_setting(con, "halt_new_entries", "1")
    e = make_engine()
    assert e.mode(broker.account())[0] == "halted"
    d = decide(e, broker, sell(qty=50))
    assert d.allow and d.intent == sell(qty=50)


# ------------------------------------------------------------- drawdown
def test_normal_mode_below_reduce_threshold(make_engine, broker, realize):
    realize("other", -9_000)  # 18% drawdown < 20%
    e = make_engine()
    assert e.mode(broker.account())[0] == "normal"
    d = decide(e, broker, buy(qty=50))
    assert d.allow and d.intent.qty == 50


def test_reduced_mode_halves(make_engine, broker, realize):
    realize("other", -10_500)  # equity 39,500, drawdown 21%
    e = make_engine()
    assert e.mode(broker.account())[0] == "reduced"
    d = decide(e, broker, buy(qty=50))
    assert d.allow and d.intent.qty == 25
    assert "halved" in d.reason and "halved" in d.intent.reason
    assert d.intent.client_order_id == "c1"


def test_reduced_mode_halves_notional(make_engine, broker, realize):
    realize("other", -10_500)
    d = decide(make_engine(), broker, buy(notional=4_000))
    assert d.allow and d.intent.notional == pytest.approx(2_000)


def test_halted_mode_blocks_buy(make_engine, broker, realize):
    realize("other", -18_000)  # drawdown 36% >= 35%
    e = make_engine()
    mode, why = e.mode(broker.account())
    assert mode == "halted" and "drawdown" in why
    d = decide(e, broker, buy(qty=1))
    assert not d.allow and "halted" in d.reason


def test_halted_but_sell_allowed(make_engine, broker, hold, realize):
    hold("AAPL", "breakout", 50)
    realize("other", -18_000)
    e = make_engine()
    assert e.mode(broker.account())[0] == "halted"
    d = decide(e, broker, sell(qty=50))
    assert d.allow and d.intent.qty == 50


def test_hwm_uses_ledger_equity_and_persists(make_engine, broker, hold):
    hold("AAPL", "breakout", 400)  # 40k position
    broker.prices["AAPL"] = 125.0  # +10k unrealized -> system equity 60k
    e1 = make_engine()
    assert e1.system_equity(broker.account()) == pytest.approx(60_000)
    assert e1.mode(broker.account())[0] == "normal"
    assert e1.high_water_mark() == pytest.approx(60_000)
    broker.prices["AAPL"] = 90.0  # equity 46k: 23% below the 60k peak
    e2 = make_engine()  # fresh engine, same database: peak must survive
    assert e2.high_water_mark() == pytest.approx(60_000)
    mode, why = e2.mode(broker.account())
    assert mode == "reduced" and "23" in why
    assert e2.high_water_mark() == pytest.approx(60_000)  # never lowered


def test_system_equity_ignores_broker_equity(make_engine, broker, hold):
    broker.cash = 100_000.0  # paper account is much richer than the budget
    e = make_engine()
    snap = broker.account()
    assert snap.equity == 100_000.0
    assert e.system_equity(snap) == pytest.approx(50_000)
    d = decide(e, broker, buy(qty=200))  # 20k asked; cap is 25% of 50k, not of 100k
    assert d.allow and d.intent.qty == 125


# ---------------------------------------------------------- per-order cap
def test_per_order_cap_blocks_oversized(make_engine, broker):
    e = make_engine(max_order_notional_pct=0.05)  # 2,500
    d = decide(e, broker, buy(qty=50))  # 5,000
    assert d.allow and d.intent.qty == 25
    assert "per-order" in d.reason and "per-order" in d.intent.reason


def test_per_order_cap_allows_small(make_engine, broker):
    d = decide(make_engine(max_order_notional_pct=0.05), broker, buy(qty=20))
    assert d.allow and d.intent.qty == 20 and d.reason == "ok"


def test_notional_order_resized_to_cap(make_engine, broker):
    d = decide(make_engine(), broker, buy(notional=30_000))
    assert d.allow and d.intent.notional == pytest.approx(12_500) and d.intent.qty is None


def test_whole_shares_stay_whole(make_engine, broker):
    d = decide(make_engine(), broker, buy(qty=200), ref=99.0)  # 12,500/99 = 126.26
    assert d.allow and d.intent.qty == 126


def test_resize_to_nothing_is_blocked(make_engine, broker):
    d = decide(make_engine(max_order_notional_pct=0.001), broker, buy(qty=10), ref=100.0)  # cap $50 = half a share
    assert not d.allow and "nothing" in d.reason


# ----------------------------------------------------------- per-position
def test_per_position_cap_resizes(make_engine, broker, hold):
    hold("AAPL", "breakout", 100)  # 10k held; cap 12.5k -> room 2.5k
    d = decide(make_engine(), broker, buy(qty=50))
    assert d.allow and d.intent.qty == 25 and "per-position" in d.reason


def test_per_position_cap_blocks_when_full(make_engine, broker, hold):
    hold("AAPL", "breakout", 125)
    d = decide(make_engine(), broker, buy(qty=1))
    assert not d.allow and "per-position" in d.reason


def test_per_position_cap_allows_other_symbol(make_engine, broker, hold):
    hold("AAPL", "breakout", 125)
    d = decide(make_engine(), broker, buy("MSFT", qty=100))
    assert d.allow and d.intent.qty == 100


# ------------------------------------------------------------------ gross
def test_gross_cap_blocks_when_full(make_engine, broker, hold):
    e = make_engine(max_gross_exposure_pct=0.4)  # 20k
    hold("AAPL", "breakout", 100)
    hold("MSFT", "pullback", 100)
    d = decide(e, broker, buy("NVDA", qty=10, strategy="rotation"))
    assert not d.allow and "gross" in d.reason


def test_gross_cap_resizes_to_room(make_engine, broker, hold):
    e = make_engine(max_gross_exposure_pct=0.4)
    hold("AAPL", "breakout", 100)
    d = decide(e, broker, buy("NVDA", qty=120, strategy="rotation"))  # room 10k
    assert d.allow and d.intent.qty == 100 and "gross" in d.reason


def test_gross_cap_allows_within(make_engine, broker, hold):
    hold("AAPL", "breakout", 100)
    d = decide(make_engine(max_gross_exposure_pct=0.4), broker, buy("NVDA", qty=50, strategy="rotation"))
    assert d.allow and d.intent.qty == 50 and d.reason == "ok"


# ----------------------------------------------------------------- crypto
CRYPTO_ONLY = dict(max_position_pct=1.0, max_order_notional_pct=1.0)  # isolate the crypto cap


def test_crypto_cap_blocks_when_full(make_engine, broker, hold):
    hold("BTC/USD", "crypto", 0.3)  # 15k = 30% of 50k
    d = decide(make_engine(**CRYPTO_ONLY), broker, buy("ETH/USD", qty=1, strategy="crypto"), ref=2_000.0)
    assert not d.allow and "crypto" in d.reason


def test_crypto_cap_resizes_to_room(make_engine, broker, hold):
    hold("BTC/USD", "crypto", 0.2)  # 10k held, room 5k
    d = decide(make_engine(**CRYPTO_ONLY), broker, buy("ETH/USD", qty=5, strategy="crypto"), ref=2_000.0)
    assert d.allow and d.intent.qty == pytest.approx(2.5) and "crypto" in d.reason


def test_crypto_cap_allows_within_and_ignores_equities(make_engine, broker, hold):
    e = make_engine(**CRYPTO_ONLY)
    assert decide(e, broker, buy("ETH/USD", qty=5, strategy="crypto"), ref=2_000.0).intent.qty == 5
    hold("BTC/USD", "crypto", 0.3)  # crypto cap exhausted, equities unaffected
    assert decide(e, broker, buy("AAPL", qty=100)).allow


# ------------------------------------------------------------- price band
def test_price_band_blocks_equity_limit_too_far(make_engine, broker):
    d = decide(make_engine(), broker, buy(qty=10, order_type=OrderType.LIMIT, limit_price=115.0))
    assert not d.allow and "price band" in d.reason


def test_price_band_allows_equity_limit_close(make_engine, broker):
    d = decide(make_engine(), broker, buy(qty=10, order_type=OrderType.LIMIT, limit_price=105.0))
    assert d.allow


def test_price_band_wider_for_crypto(make_engine, broker):
    e = make_engine()
    far = buy("BTC/USD", qty=0.01, strategy="crypto", order_type=OrderType.LIMIT, limit_price=62_500.0)
    near = buy("BTC/USD", qty=0.01, strategy="crypto", order_type=OrderType.LIMIT, limit_price=57_500.0)  # +15%
    assert not decide(e, broker, far, ref=50_000.0).allow
    assert decide(e, broker, near, ref=50_000.0).allow
    # the same 15% would be rejected for an equity
    assert not decide(e, broker, buy(qty=10, order_type=OrderType.LIMIT, limit_price=115.0)).allow


def test_price_band_uses_live_market_price_for_buys(make_engine, broker):
    e = make_engine()
    assert not decide(e, broker, buy(qty=10), market_price=120.0).allow
    assert decide(e, broker, buy(qty=10), market_price=104.0).allow


def test_price_band_applies_to_sell_limit(make_engine, broker, hold):
    hold("AAPL", "breakout", 50)
    e = make_engine()
    assert not decide(e, broker, sell(qty=50, order_type=OrderType.LIMIT, limit_price=140.0)).allow
    assert decide(e, broker, sell(qty=50, order_type=OrderType.LIMIT, limit_price=104.0)).allow


def test_gap_down_market_sell_and_far_stop_not_blocked(make_engine, broker, hold):
    hold("AAPL", "breakout", 50)
    broker.prices["AAPL"] = 70.0  # gapped 30% below the reference
    e = make_engine()
    assert decide(e, broker, sell(qty=50)).allow
    assert decide(e, broker, sell(qty=50, order_type=OrderType.STOP, stop_price=70.0)).allow


# --------------------------------------------------------- orders per run
def test_max_orders_per_run_blocks_buy(make_engine, broker):
    d = decide(make_engine(), broker, buy(qty=10), orders=20)
    assert not d.allow and "orders" in d.reason


def test_max_orders_per_run_allows_below(make_engine, broker):
    assert decide(make_engine(), broker, buy(qty=10), orders=19).allow


def test_sell_not_limited_by_order_count(make_engine, broker, hold):
    hold("AAPL", "breakout", 50)
    assert decide(make_engine(), broker, sell(qty=50), orders=500).allow


# --------------------------------------------------------- sleeve breaker
def test_sleeve_breaker_blocks_losing_strategy(make_engine, broker, realize):
    realize("breakout", -4_500)  # 9% of budget > 8%
    e = make_engine()
    assert e.mode(broker.account())[0] == "normal"  # system itself is fine
    d = decide(e, broker, buy(qty=10, strategy="breakout"))
    assert not d.allow and "circuit breaker" in d.reason


def test_sleeve_breaker_allows_other_strategy_and_small_loss(make_engine, broker, realize):
    realize("breakout", -4_500)
    realize("rotation", -3_000)  # 6%
    e = make_engine()
    assert decide(e, broker, buy(qty=10, strategy="pullback")).allow
    assert decide(e, broker, buy("MSFT", qty=10, strategy="rotation")).allow


def test_sleeve_breaker_measured_from_peak(make_engine, broker, realize):
    e = make_engine()
    realize("breakout", 5_000)
    assert decide(e, broker, buy(qty=10)).allow  # records the 5,000 peak
    realize("breakout", -4_500)  # pnl 500: 9% below peak
    d = decide(e, broker, buy("MSFT", qty=10))
    assert not d.allow and "circuit breaker" in d.reason


def test_sleeve_breaker_sell_still_allowed(make_engine, broker, hold, realize):
    hold("AAPL", "breakout", 50)
    realize("breakout", -4_500)
    assert decide(make_engine(), broker, sell(qty=50)).allow


def test_sleeve_breaker_reset(make_engine, broker, realize):
    realize("breakout", -4_500)
    e = make_engine()
    assert not decide(e, broker, buy(qty=10)).allow
    e.reset_sleeve_peak("breakout", broker.account())
    assert decide(e, broker, buy(qty=10)).allow


# ------------------------------------------------------ no shorting / sells
def test_oversell_blocked(make_engine, broker, hold):
    hold("AAPL", "breakout", 50)
    d = decide(make_engine(), broker, sell(qty=51))
    assert not d.allow and d.intent is None and "exceeds held" in d.reason


def test_sell_without_position_blocked(make_engine, broker):
    d = decide(make_engine(), broker, sell("MSFT", qty=1))
    assert not d.allow and "no shorting" in d.reason


def test_sell_up_to_held_allowed(make_engine, broker, hold):
    hold("AAPL", "breakout", 50)
    e = make_engine()
    assert decide(e, broker, sell(qty=50)).allow
    assert decide(e, broker, sell(qty=10)).allow
    assert decide(e, broker, sell(notional=2_000)).allow  # 20 shares at ref 100
    assert not decide(e, broker, sell(notional=6_000)).allow  # 60 shares


def test_crypto_sell_dust_tolerance(make_engine, broker, hold):
    hold("BTC/USD", "crypto", 0.1)
    e = make_engine()
    assert decide(e, broker, sell("BTC/USD", qty=0.1 + 5e-7), ref=50_000.0).allow
    assert not decide(e, broker, sell("BTC/USD", qty=0.1 + 5e-5), ref=50_000.0).allow


def test_sell_is_never_resized(make_engine, broker, hold, realize):
    hold("AAPL", "breakout", 50)
    realize("other", -10_500)  # reduced mode
    d = decide(make_engine(max_order_notional_pct=0.01), broker, sell(qty=50))
    assert d.allow and d.intent.qty == 50


# ------------------------------------------------------------- misc rules
def test_one_symbol_one_strategy_blocks_second_owner(make_engine, broker, hold):
    hold("AAPL", "pullback", 10)
    d = decide(make_engine(), broker, buy(qty=10, strategy="breakout"))
    assert not d.allow and "pullback" in d.reason
    assert decide(make_engine(), broker, buy(qty=10, strategy="pullback")).allow


def test_trading_blocked_snapshot_blocks_buy_not_sell(make_engine, broker, hold):
    hold("AAPL", "breakout", 50)
    snap = broker.account()
    snap.trading_blocked = True
    e = make_engine()
    assert not e.check(buy("MSFT", qty=1), snap, 100.0, 0).allow
    assert e.check(sell(qty=50), snap, 100.0, 0).allow


def test_unknown_broker_position_counts_toward_exposure(make_engine, broker):
    # broker holds 12.5k of something the ledger never heard of: still counted
    broker.prices["TSLA"] = 100.0
    broker.positions["TSLA"] = BrokerPosition("TSLA", AssetClass.EQUITY, 125, 100.0, 12_500, 100.0)
    d = decide(make_engine(), broker, buy("TSLA", qty=1))
    assert not d.allow and "per-position" in d.reason


def test_default_limits_match_spec():
    l = Limits()
    assert (l.capital_budget, l.max_order_notional_pct, l.max_position_pct, l.max_gross_exposure_pct,
            l.max_crypto_exposure_pct, l.max_orders_per_run, l.price_band_equity, l.price_band_crypto,
            l.dd_reduce, l.dd_halt) == (50_000, 0.25, 0.25, 1.0, 0.30, 20, 0.10, 0.20, 0.20, 0.35)


def test_resized_intent_is_new_object_and_original_untouched(make_engine, broker):
    original = buy(qty=500, reason="signal: breakout")
    d = decide(make_engine(), broker, original)
    assert d.intent is not original and original.qty == 500
    assert d.intent.reason.startswith("signal: breakout") and "risk resized" in d.intent.reason
