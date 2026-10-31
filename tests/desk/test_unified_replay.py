import json
from dataclasses import replace
from datetime import date, datetime, timedelta

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


def test_grok_and_spreads_are_explicitly_not_replayable(tmp_path):
    proposal = {"sleeve": "options", "kind": "param", "param": {"key": "options.grok_plan.threshold", "new": 1}}
    result = replay.run("options", "2026-10-14", "2026-10-14", proposal, cache_path=tmp_path / "c.sqlite")
    assert result["status"] == "not_replayable" and result["n"] == 0
    assert "not_replayable" in result["caveats"]
    spreads = replay.run("spreads", "2026-10-14", "2026-10-14", cache_path=tmp_path / "c.sqlite")
    assert spreads.keys() == result.keys()
    assert spreads["status"] == "not_replayable"
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


def test_cli_uses_same_shape(tmp_path):
    from scripts.desk_replay import main
    path = tmp_path / "out.json"
    main(["--sleeve", "spreads", "--from", "2026-10-14", "--to", "2026-10-14",
          "--out", str(path), "--cache", str(tmp_path / "cache.sqlite")])
    assert json.loads(path.read_text())["tier"] == "replay"
