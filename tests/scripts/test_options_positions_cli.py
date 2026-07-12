"""1E of ~/.claude/plans/buzzing-yawning-squid.md: confirm `check` actually
threads production module A/B's live regime/EV into the risk assessment,
rather than silently skipping those two triggers (options_risk.py's
regime_invalidation / dynamic_ev_take_profit notes) every time. Reuses
test_production.py's synthetic-history seeding pattern -- no real ingested
data needed, just enough synthetic history for GMM+logistic to fit."""
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import options_positions_cli  # noqa: E402
from stockmoney.data.db import append_rows, run_migrations  # noqa: E402
from stockmoney.data.positions import open_position  # noqa: E402
from stockmoney.models.feature_matrix import FEATURE_COLUMNS  # noqa: E402
from stockmoney.models.production import ProductionPrediction  # noqa: E402

TARGET = "SOXL"
SECTOR = "semiconductor"


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    # A single ohlcv_daily row so latest_underlying_price has something to
    # return -- production.predict_latest/current_ev_of_continuing themselves
    # are mocked below, since this test is about whether the CLI *threads*
    # their return values into MarketSnapshot, not about whether production's
    # own walk-forward has enough synthetic history to produce a non-None EV
    # (test_production.py already covers -- and honestly caveats -- that).
    append_rows(conn, "ohlcv_daily", pl.DataFrame({
        "symbol": [TARGET], "trade_date": [date(2024, 1, 1)], "close": [100.0],
        "source": ["test"], "ingested_at": [datetime(2024, 1, 1, 21, tzinfo=timezone.utc)],
    }))
    return conn


def _fake_prediction(regime: int) -> ProductionPrediction:
    return ProductionPrediction(
        as_of_date=date(2024, 1, 1), symbol=TARGET, sector=SECTOR, horizon=5,
        regime=regime, proba=np.array([0.3, 0.4, 0.3]),
        feature_values={c: 0.0 for c in FEATURE_COLUMNS}, model_version="test-v0",
    )


def test_check_autofills_regime_and_ev_from_production_when_not_passed(monkeypatch, capsys):
    conn = _conn()
    position_id = open_position(
        conn, symbol=TARGET, option_right="call", side="long", strike=110.0,
        expiry_date=date(2024, 2, 1), entry_date=date(2024, 1, 1),
        entry_underlying_price=100.0, entry_premium=5.0, entry_iv=0.4,
        regime_at_entry=0,  # deliberately mismatched vs the mocked "current" regime (2) below
    )

    monkeypatch.setattr(options_positions_cli, "get_connection", lambda db_path: conn)
    monkeypatch.setattr(
        options_positions_cli.production, "predict_latest",
        lambda conn, *, target_symbol, sector, **kw: _fake_prediction(regime=2),
    )
    monkeypatch.setattr(
        options_positions_cli.production, "current_ev_of_continuing",
        lambda conn, *, target_symbol, sector, asof_date=None: -0.05,
    )

    exit_code = options_positions_cli.main(["check", "--position-id", position_id])
    out = capsys.readouterr().out

    assert exit_code == 0
    # Neither production-dependent trigger is reported as "skipped for missing
    # data" -- proving current_regime/ev_of_continuing were populated from the
    # (mocked) production calls, not left None. (premium_stop/take_profit are
    # separately skipped here since --current-premium wasn't passed -- that's
    # expected and unrelated to production wiring.)
    assert "regime_invalidation not evaluated" not in out
    assert "dynamic_ev_take_profit not evaluated" not in out
    # And the populated values actually drove real triggers: entry regime (0)
    # != mocked current regime (2) -> invalidation fires; mocked EV (-0.05) < 0
    # -> the dynamic take-profit fires.
    assert "regime reclassified since entry (was 0, now 2)" in out
    assert "expected value of continuing to hold is negative" in out


def test_check_respects_explicit_overrides_over_production(monkeypatch, capsys):
    # Passing --current-regime/--ev-of-continuing explicitly should short-circuit
    # the production auto-fill entirely (CLI already guards this with `if
    # current_regime is None or ev_of_continuing is None`).
    conn = _conn()
    position_id = open_position(
        conn, symbol=TARGET, option_right="call", side="long", strike=110.0,
        expiry_date=date(2024, 2, 1), entry_date=date(2024, 1, 1),
        entry_underlying_price=100.0, entry_premium=5.0, entry_iv=0.4,
        regime_at_entry=1,
    )

    monkeypatch.setattr(options_positions_cli, "get_connection", lambda db_path: conn)
    exit_code = options_positions_cli.main([
        "check", "--position-id", position_id,
        "--current-regime", "1", "--ev-of-continuing", "0.02",
    ])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "regime reclassified" not in out  # regime_at_entry(1) == override(1) -> no trigger
    assert "expected value of continuing to hold is negative" not in out  # 0.02 >= 0


def test_check_skips_autofill_for_non_watchlist_symbol(monkeypatch, capsys):
    conn = _conn()
    position_id = open_position(
        conn, symbol="ZZZZ", option_right="put", side="long", strike=10.0,
        expiry_date=date(2026, 1, 1), entry_date=date(2025, 1, 1),
        entry_underlying_price=12.0, entry_premium=1.0,
    )
    monkeypatch.setattr(options_positions_cli, "get_connection", lambda db_path: conn)
    exit_code = options_positions_cli.main([
        "check", "--position-id", position_id, "--underlying-price", "9.0",
    ])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "auto-fill skipped: ZZZZ is not an active watchlist member" in out
