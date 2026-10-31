import json
from dataclasses import replace
from datetime import date, datetime, time, timedelta

import pandas as pd
import pytest

from stockmoney.desk.data import Bars, FIELDS
from stockmoney.desk.learn import replay
from stockmoney.desk.options.config import OptionsConfig
from stockmoney.desk.options import replay as options_replay
from test_desk import synthetic_bars


@pytest.fixture(scope="module")
def bars():
    return synthetic_bars(n=400, seed=7)


def test_stock_shape_costs_and_holdout_cache(tmp_path, bars):
    cache = tmp_path / "cache.sqlite"
    result = replay.run("stocks", "2021-01-01", "2021-07-13", bars=bars, cache_path=cache)
    assert result["tier"] == "replay" and result["n"] > 0
    assert result["n"] == len(result["trades"])
    assert result["costs"]["stock_bps_per_side"] == 5
    assert "earnings_calendar_not_pit" in result["caveats"]
    assert result["max_drawdown"] >= 0
    # No bars supplied: cache must return the previously frozen evidence.
    assert replay.run("stocks", "2021-01-01", "2021-07-13", cache_path=cache) == result
    assert replay.run("stocks", "2021-01-01", "2021-07-13", cache_path=cache, feed="sip",
                      options_cfg=replace(OptionsConfig(), report_dir=tmp_path, fee_per_contract=0.1)) == result
    higher_cost = replay.run("stocks", "2021-01-01", "2021-07-13", bars=bars, cache_path=cache, stock_bps=20)
    assert higher_cost["variant_hash"] != result["variant_hash"]
    assert higher_cost["trades"] != result["trades"]


def test_future_spike_cannot_change_prior_stock_decisions(tmp_path, bars):
    spike = bars.dates[-20]
    changed = Bars(*(getattr(bars, field).copy() for field in FIELDS))
    for field in FIELDS:
        getattr(changed, field).loc[spike:] *= 50
    a = replay.run("stocks", "2021-01-01", "2021-07-13", bars=bars, cache_path=tmp_path / "a.sqlite")
    b = replay.run("stocks", "2021-01-01", "2021-07-13", bars=changed, cache_path=tmp_path / "b.sqlite")
    before = lambda result: [d for d in result["decisions"] if pd.Timestamp(d["at"]).date() < spike.date()]
    assert before(a) and before(a) == before(b)


@pytest.mark.parametrize("bps", [0, -1, float("nan"), float("inf")])
def test_zero_or_invalid_costs_refused_before_cache_write(tmp_path, bps):
    cache = tmp_path / "bad.sqlite"
    with pytest.raises(ValueError, match="cost"):
        replay.run("stocks", "2026-01-01", "2026-01-02", cache_path=cache, stock_bps=bps)
    assert not cache.exists()


def test_zero_option_cost_refused(tmp_path):
    with pytest.raises(ValueError, match="positive fees"):
        replay.run("options", "2026-10-14", "2026-10-14", cache_path=tmp_path / "bad.sqlite",
                   options_cfg=replace(OptionsConfig(), fee_per_contract=0))


def test_proposal_cache_ignores_cosmetic_hypothesis_changes(tmp_path, bars):
    proposal = {"sleeve": "stocks", "kind": "param", "param": {"key": "pullback.trail_atr", "new": 3.5}}
    a = replay.run("stocks", "2021-01-01", "2021-07-13", proposal, bars=bars, cache_path=tmp_path / "c.sqlite")
    b = replay.run("stocks", "2021-01-01", "2021-07-13", {**proposal, "hypothesis": "cosmetic rewrite"},
                   cache_path=tmp_path / "c.sqlite")
    assert a == b


def test_grok_is_explicitly_not_replayable(tmp_path):
    proposal = {"sleeve": "options", "kind": "param", "param": {"key": "options.grok_plan.threshold", "new": 1}}
    result = replay.run("options", "2026-10-14", "2026-10-14", proposal, cache_path=tmp_path / "c.sqlite")
    assert result["status"] == "not_replayable" and result["n"] == 0
    assert "not_replayable" in result["caveats"]
    proposal["param"]["key"] = "options.grok_min_rvol"
    assert replay.run("options", "2026-10-14", "2026-10-14", proposal,
                      cache_path=tmp_path / "c.sqlite")["status"] == "not_replayable"
    assert not (tmp_path / "c.sqlite").exists()


def test_options_shared_engine_shape_and_future_bar_isolation(tmp_path, bars):
    day = date(2026, 10, 14)
    cache = tmp_path / "options"
    options_replay.synthetic_cache(cache, day)
    cfg = OptionsConfig(index=("SPY",), mega_cap=(), high_vol=())
    result = replay.run("options", str(day), str(day), options_cache=cache,
                        cache_path=tmp_path / "evidence.sqlite", options_cfg=cfg)
    stock = replay.run("stocks", "2021-01-01", "2021-07-13", bars=bars, cache_path=tmp_path / "stock.sqlite")
    assert result.keys() == stock.keys()
    assert result["n"] > 0 and all(t["fees"] > 0 for t in result["trades"])
    assert "synthetic_quotes" in result["caveats"]
    assert "snapshot_iv_not_pit" in result["caveats"]
    assert result["max_drawdown"] > 0
    assert replay.run("options", str(day), str(day), options_cache=tmp_path / "missing",
                      cache_path=tmp_path / "evidence.sqlite", stock_bps=99,
                      options_cfg=replace(cfg, report_dir=tmp_path, halt_file=tmp_path / "unused")) == result
    market = options_replay.ReplayMarket(cache / str(day), "iex", cfg)
    now = datetime(2026, 10, 14, 10, 0, tzinfo=options_replay.ET)
    clock = options_replay.SimClock(now)
    broker = options_replay.ReplayBroker(market, clock)
    before = broker.minute_bars("SPY", now - timedelta(minutes=30), now)
    spot = market.spot("SPY", now)
    market.stock["SPY"].loc[pd.Timestamp(now):, "close"] *= 100
    pd.testing.assert_frame_equal(before, broker.minute_bars("SPY", now - timedelta(minutes=30), now))
    assert market.spot("SPY", now) == spot
    assert (before.index < pd.Timestamp(now)).all()


def test_cli_uses_same_shape(tmp_path, monkeypatch):
    import scripts.desk_replay as cli
    monkeypatch.setattr(cli.data, "load", lambda: "long")
    monkeypatch.setattr(cli.data, "to_bars", lambda long: "bars")
    monkeypatch.setattr(replay, "_wind_states", lambda bars, start, end: {})
    path = tmp_path / "out.json"
    cli.main(["--sleeve", "spreads", "--from", "2026-10-14", "--to", "2026-10-14",
              "--out", str(path), "--cache", str(tmp_path / "cache.sqlite"),
              "--options-cache", str(tmp_path / "none")])
    out = json.loads(path.read_text())
    assert out["tier"] == "replay" and out["status"] == "ok" and out["n"] == 0
    assert "missing_cache_days" in out["caveats"]


# ------------------------------------------------------------------ spreads (issue 37)

D0, D1, EXPIRY = date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 28)
SPREAD_CFG = OptionsConfig(index=("SPY",), mega_cap=(), high_vol=(), default_iv=0.15)


def _spread_cache(root, path):
    """Synthetic SPY minute bars (``path``: day -> [(minute after 09:30, price)],
    linear between points) and 14-DTE calls 580 to 630 with no option bars, so
    every leg is quoted by the Black-Scholes model at ``default_iv``."""
    from stockmoney.desk.options._iface import occ_symbol
    for day, pts in path.items():
        d = root / str(day)
        d.mkdir(parents=True)
        rows = []
        for m in range(390):
            px = next((p0 + (p1 - p0) * (m - m0) / (m1 - m0)
                       for (m0, p0), (m1, p1) in zip(pts, pts[1:]) if m0 <= m <= m1), pts[-1][1])
            t = datetime.combine(day, time(9, 30), options_replay.ET) + timedelta(minutes=m)
            rows.append((t, px, px + 0.05, px - 0.05, px, 100_000))
        df = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "volume"])
        df["vwap"], df["symbol"], df["t"] = df.close, "SPY", pd.to_datetime(df["t"], utc=True)
        for feed in ("sip", "iex"):
            df.to_parquet(d / f"stock_{feed}.parquet", index=False)
        pd.DataFrame([{"occ": occ_symbol("SPY", EXPIRY, "C", k), "underlying": "SPY", "right": "C",
                       "strike": float(k), "expiry": str(EXPIRY), "open_interest": 5000,
                       "open_interest_date": str(day)} for k in range(580, 631)]
                     ).to_parquet(d / "contracts.parquet", index=False)
        pd.DataFrame(columns=["occ", "t", "o", "h", "l", "c", "v", "vw"]).to_parquet(
            d / "option_bars.parquet", index=False)
        (d / "meta.json").write_text(json.dumps({"day": str(day), "synthetic": True, "symbols": ["SPY"]}))
    return root


@pytest.fixture
def spread_day(tmp_path):
    # flat at 600 on D0 (entry), rally to 620 in the first hour of D1 (exit)
    return _spread_cache(tmp_path / "opt", {D0: [(0, 600.0), (389, 600.0)],
                                            D1: [(0, 600.0), (60, 620.0), (389, 620.0)]})


def _quote(cache, day, strike, hh, mm):
    from stockmoney.desk.options._iface import occ_symbol
    market = options_replay.ReplayMarket(cache / str(day), "iex", SPREAD_CFG)
    now = datetime.combine(day, time(hh, mm), options_replay.ET)
    return market.quote(occ_symbol("SPY", EXPIRY, "C", strike), now)[0]


def test_spreads_known_day_legs_debit_and_exit(tmp_path, spread_day, monkeypatch):
    monkeypatch.setattr(replay, "_wind_states", lambda bars, start, end: {"2026-10-13": "tailwind",
                                                                          "2026-10-14": "neutral"})
    result = replay.run("spreads", str(D0), str(D1), bars="bars", options_cache=spread_day,
                        cache_path=tmp_path / "evidence.sqlite", options_cfg=SPREAD_CFG)
    assert result["status"] == "ok" and result["n"] == 1
    spy = [d for d in result["decisions"] if d["underlying"] == "SPY"]
    assert len(spy) == 1
    row = spy[0]
    assert (row["status"], row["wind"], row["expiry"]) == ("CLOSED", "tailwind", str(EXPIRY))
    assert (row["long_strike"], row["short_strike"], row["qty"]) == (600.0, 610.0, 2)
    # Buy the long at the ask, sell the short at the bid, SLIP on each leg, quoted at the 09:45 slot.
    long_in, short_in = _quote(spread_day, D0, 600, 9, 45), _quote(spread_day, D0, 610, 9, 45)
    debit = (long_in.ask + 0.03) - (short_in.bid - 0.03)
    assert row["entry_debit"] == pytest.approx(debit) == pytest.approx(4.07)
    # Exits start the next session: take profit at the first slot worth debit + half the max profit.
    trade = result["trades"][0]
    assert trade["entry_date"] == "2026-10-14T13:45:00+00:00"
    assert trade["exit_date"] == "2026-10-15T14:25:00+00:00" and trade["exit_reason"] == "take_profit"
    long_out, short_out = _quote(spread_day, D1, 600, 10, 25), _quote(spread_day, D1, 610, 10, 25)
    value = (long_out.bid - 0.03) - (short_out.ask + 0.03)
    assert value >= debit + 0.5 * (10 - debit)
    fees = 4 * 2 * SPREAD_CFG.fee_per_contract
    assert trade["fees"] == pytest.approx(fees)
    assert trade["pnl"] == pytest.approx((value - debit) * 100 * 2 - fees)
    assert trade["r_multiple"] == pytest.approx(trade["pnl"] / (debit * 100 * 2))
    # QQQ has no chain in the cache: skipped on the last entry slot with the live reason.
    assert {(d["underlying"], d["skip_reason"]) for d in result["decisions"] if d["status"] == "SKIPPED"} == {
        ("QQQ", "no_expiry")}
    assert "synthetic_quotes" in result["caveats"] and "not_replayable" not in result["caveats"]
    assert "spreads_replay_pending_issue_37" not in result["caveats"]
    # Frozen: the cached evidence comes back without bars or a cache directory.
    assert replay.run("spreads", str(D0), str(D1), options_cache=tmp_path / "missing",
                      cache_path=tmp_path / "evidence.sqlite", options_cfg=SPREAD_CFG) == result


def test_spreads_only_on_tailwind(tmp_path, spread_day, monkeypatch):
    monkeypatch.setattr(replay, "_wind_states", lambda bars, start, end: {"2026-10-13": "neutral",
                                                                          "2026-10-14": "tailwind"})
    result = replay.run("spreads", str(D0), str(D0), bars="bars", options_cache=spread_day,
                        cache_path=tmp_path / "evidence.sqlite", options_cfg=SPREAD_CFG)
    assert result["n"] == 0 and result["decisions"] == []


def test_spreads_extra_slippage_widens_the_debit(tmp_path, spread_day, monkeypatch):
    monkeypatch.setattr(replay, "_wind_states", lambda bars, start, end: {"2026-10-13": "tailwind"})
    cfg = replace(SPREAD_CFG, shadow_slippage_pct=0.05)
    result = replay.run("spreads", str(D0), str(D0), bars="bars", options_cache=spread_day,
                        cache_path=tmp_path / "evidence.sqlite", options_cfg=cfg)
    row = next(d for d in result["decisions"] if d["underlying"] == "SPY")
    long_in, short_in = _quote(spread_day, D0, 600, 9, 45), _quote(spread_day, D0, 610, 9, 45)
    assert row["entry_debit"] == pytest.approx(long_in.ask * 1.05 - short_in.bid * 0.95)
    assert "open_spreads_marked_in_drawdown_only" in result["caveats"]


def test_spreads_requires_bars_for_the_wind(tmp_path):
    with pytest.raises(ValueError, match="daily bars"):
        replay.run("spreads", str(D0), str(D0), cache_path=tmp_path / "evidence.sqlite")


def test_spreads_wind_states_are_causal(bars):
    start, end = str(bars.dates[-30].date()), str(bars.dates[-1].date())
    full = replay._wind_states(bars, start, end)
    cut = str(bars.dates[-15].date())
    early = replay._wind_states(bars.upto(pd.Timestamp(cut)), start, cut)
    assert early and all(full[d] == s for d, s in early.items())
