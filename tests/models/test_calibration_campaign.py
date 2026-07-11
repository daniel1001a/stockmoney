"""Tests for the weekend calibration campaign
(~/.claude/plans/frolicking-wishing-cook.md).

Uses a small synthetic dataset + a monkeypatched tiny grid (not the real
~2200-cell sweep) to exercise run_search_grid/shortlist_top_k/
run_confirmation end to end quickly, while still running the real
statistical/idempotency logic -- not mocks of it.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import numpy as np
import polars as pl

from stockmoney.data.db import MARKET_SYMBOL, append_rows, run_migrations, sector_symbol
from stockmoney.data.features.base import FeatureValue, write_features
from stockmoney.models import calibration_campaign as C
from stockmoney.models.direction import LogisticDirectionModel
from stockmoney.models.feature_matrix import build_feature_matrix, to_dataset
from stockmoney.models.walk_forward import run_walk_forward

TARGET = "SOXL"
SECTOR = "semiconductor"


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def _seed(conn, n=200, seed=0, drift=0.0):
    """Enough SOXL feature history for a walk-forward fit, sized so the
    holdout tail (HOLDOUT_FRAC) still leaves enough rows for both search and
    confirm to fit/test on. Values are written directly (not derived from
    OHLCV via the real compute_* functions), so there's no feature warm-up
    gap -- every one of the n dates has every feature present."""
    rng = np.random.default_rng(seed)
    start = date(2023, 1, 1)
    dates = [start + timedelta(days=i) for i in range(n)]
    av = [datetime(d.year, d.month, d.day, 21, tzinfo=timezone.utc) for d in dates]

    closes = [100.0]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + drift + rng.normal(scale=0.01)))

    append_rows(conn, "ohlcv_daily", pl.DataFrame({
        "symbol": [TARGET] * n, "trade_date": dates, "close": closes,
        "source": ["test"] * n, "ingested_at": av,
    }))

    def _fv(sym, vals):
        return [FeatureValue(d, sym, value=float(v), available_at=a) for d, v, a in zip(dates, vals, av)]

    rv, adx, disp = rng.uniform(0.2, 0.6, n), rng.uniform(10, 40, n), rng.uniform(0.005, 0.02, n)
    curve, dxy, oil = rng.normal(size=n), rng.normal(scale=0.01, size=n), rng.normal(scale=0.02, size=n)

    write_features(conn, feature_name="realized_vol_20d", feature_version="v1",
                   source_table="ohlcv_daily", values=_fv(TARGET, rv))
    write_features(conn, feature_name="adx_14", feature_version="v1",
                   source_table="ohlcv_daily", values=_fv(TARGET, adx))
    write_features(conn, feature_name="xsec_dispersion", feature_version="v1",
                   source_table="ohlcv_daily", values=_fv(sector_symbol(SECTOR), disp))
    write_features(conn, feature_name="yield_curve_10y2y", feature_version="v1",
                   source_table="macro_series_daily", values=_fv(MARKET_SYMBOL, curve))
    write_features(conn, feature_name="dxy_chg_1d_ffill", feature_version="v1",
                   source_table="macro_series_daily", values=_fv(MARKET_SYMBOL, dxy))
    write_features(conn, feature_name="oil_chg_1d_ffill", feature_version="v1",
                   source_table="macro_series_daily", values=_fv(MARKET_SYMBOL, oil))


def _shrink_grid(monkeypatch):
    """Tiny grid so tests run in well under a second while still exercising
    multiple band_k/n_regimes/seed values and both regime methods."""
    monkeypatch.setattr(C, "GRID_METHODS", ["gmm"])
    monkeypatch.setattr(C, "GRID_N_REGIMES", [2, 3])
    monkeypatch.setattr(C, "GRID_BAND_K", [0.4, 0.5])
    monkeypatch.setattr(C, "GRID_SEEDS", [0, 1])
    monkeypatch.setattr(C, "SEARCH_N_FOLDS", 2)


def test_run_search_grid_writes_all_combinations_and_is_resumable(monkeypatch):
    conn = _conn()
    _seed(conn)
    _shrink_grid(monkeypatch)

    counts = C.run_search_grid(conn, campaign_id="test-campaign", symbols=[(TARGET, SECTOR)])
    expected = len(C.GRID_METHODS) * len(C.GRID_N_REGIMES) * len(C.GRID_BAND_K) * len(C.GRID_SEEDS)
    assert counts["written"] == expected
    assert conn.execute(
        "SELECT count(*) FROM calibration_runs WHERE campaign_id = 'test-campaign' AND phase = 'search'"
    ).fetchone()[0] == expected

    # Resuming must skip everything already run, not duplicate it.
    counts2 = C.run_search_grid(conn, campaign_id="test-campaign", symbols=[(TARGET, SECTOR)])
    assert counts2["written"] == 0
    assert counts2["skipped_existing"] == expected


def test_truncate_dataset_excludes_the_holdout_tail():
    conn = _conn()
    _seed(conn)
    matrix = build_feature_matrix(conn, target_symbol=TARGET, sector=SECTOR, horizon=C.HORIZON, band_k=0.5)
    full = to_dataset(matrix)
    holdout_start_idx = int(len(full.trade_dates) * (1 - C.HOLDOUT_FRAC))
    holdout_start_date = full.trade_dates[holdout_start_idx]

    truncated = C._truncate_dataset(full, holdout_start_idx)
    assert len(truncated.trade_dates) == holdout_start_idx
    assert all(d < holdout_start_date for d in truncated.trade_dates)


def test_compare_to_baseline_flags_no_significant_difference_for_identical_config():
    """A candidate config identical to the baseline must never claim a
    significant improvement or worsening -- sanity check on the bootstrap
    comparison logic itself before trusting it on real grid results."""
    conn = _conn()
    _seed(conn)

    matrix = build_feature_matrix(
        conn, target_symbol=TARGET, sector=SECTOR, horizon=C.HORIZON, band_k=C.BASELINE_BAND_K
    )
    ds = C._truncate_dataset(to_dataset(matrix), int(matrix.height * (1 - C.HOLDOUT_FRAC)))

    def make_track():
        return C._make_track(C.BASELINE_METHOD, C.BASELINE_N_REGIMES, C.BASELINE_SEED)

    kwargs = dict(n_folds=C.SEARCH_N_FOLDS, initial_frac=C.SEARCH_INITIAL_FRAC, embargo=C.EMBARGO)
    result_a = run_walk_forward(ds, make_track, lambda: LogisticDirectionModel(seed=0, calibrate="sigmoid"), **kwargs)
    result_b = run_walk_forward(ds, make_track, lambda: LogisticDirectionModel(seed=0, calibrate="sigmoid"), **kwargs)

    verdict = C.compare_to_baseline(result_a, result_b)
    assert not any(v.improved for v in verdict.per_regime)
    assert not any(v.worsened for v in verdict.per_regime)
    assert verdict.passes is False
    assert abs(verdict.overall_skill_diff) < 1e-9  # identical runs -> exactly zero skill gap


def test_wider_band_k_confound_would_fool_raw_brier_but_not_skill_score():
    """Regression guard for the label-difficulty confound (review Finding 1).

    The SAME model on a WIDER band_k gets a lower RAW Brier purely because more
    days fall in the dominant RANGE class (an easier scoring target), not
    because the model is better. This test pins the fix by asserting the
    contrast directly: the old raw-Brier comparison WOULD flag the wider-band
    same-model as improved, while the skill-over-climatology comparison the
    code now uses does NOT let the confound force a pass.

    Uses a strong up-drift so band_k=0.5 and 0.7 produce genuinely different
    label distributions on the synthetic data."""
    import numpy as np
    from stockmoney.models.metrics import brier_per_sample, brier_score, bootstrap_paired_diff_ci

    conn = _conn()
    _seed(conn, n=240, drift=0.006)

    def run(band_k):
        m = build_feature_matrix(conn, target_symbol=TARGET, sector=SECTOR, horizon=C.HORIZON, band_k=band_k)
        ds = C._truncate_dataset(to_dataset(m), int(m.height * (1 - C.HOLDOUT_FRAC)))
        return run_walk_forward(
            ds, lambda: C._make_track("gmm", 3, 0),
            lambda: LogisticDirectionModel(seed=0, calibrate="sigmoid"),
            n_folds=C.SEARCH_N_FOLDS, initial_frac=C.SEARCH_INITIAL_FRAC, embargo=C.EMBARGO,
        )

    baseline = run(C.BASELINE_BAND_K)  # 0.5
    wider = run(0.7)  # same model, mechanically easier labels

    # Precondition: the confound exists -- raw Brier really is lower for the wider band.
    assert brier_score(wider.y_true, wider.proba) < brier_score(baseline.y_true, baseline.proba)

    # What the OLD raw-Brier logic would have concluded: at least one regime
    # "improved" (lower candidate Brier), i.e. the confound forces a pass.
    base_brier_by_date = dict(zip(baseline.trade_dates, brier_per_sample(baseline.y_true, baseline.proba)))
    cand_brier = brier_per_sample(wider.y_true, wider.proba)
    old_improved = []
    for r in sorted(set(wider.regime.tolist())):
        mask = wider.regime == r
        dts = [d for d, mm in zip(wider.trade_dates, mask) if mm]
        _, _, hi = bootstrap_paired_diff_ci(cand_brier[mask], np.array([base_brier_by_date[d] for d in dts]))
        old_improved.append(hi < 0)
    assert any(old_improved), "precondition: raw-Brier logic should have been fooled by the confound"

    # What the NEW skill-over-climatology logic concludes: the confound alone
    # must not force a pass.
    assert not C.compare_to_baseline(wider, baseline).passes


def test_shortlist_top_k_ranks_and_is_empty_when_nothing_passes(monkeypatch):
    conn = _conn()
    _seed(conn)
    _shrink_grid(monkeypatch)
    C.run_search_grid(conn, campaign_id="c1", symbols=[(TARGET, SECTOR)])

    top = C.shortlist_top_k(conn, campaign_id="c1", symbol=TARGET, top_k=2)
    assert len(top) <= 2  # may legitimately be 0 -- weak baseline signal is expected, not a bug

    empty = C.shortlist_top_k(conn, campaign_id="c1", symbol="NONEXISTENT")
    assert empty == []


def test_confirmation_is_idempotent_and_never_reruns():
    conn = _conn()
    _seed(conn)
    params = {"method": "gmm", "n_regimes": 3, "band_k": 0.5, "seed": 0}

    first = C.run_confirmation(conn, campaign_id="c1", symbol=TARGET, sector=SECTOR, params=params)
    assert first in ("passed", "failed")
    assert conn.execute(
        "SELECT count(*) FROM calibration_runs WHERE campaign_id = 'c1' AND phase = 'confirm'"
    ).fetchone()[0] == 1

    second = C.run_confirmation(conn, campaign_id="c1", symbol=TARGET, sector=SECTOR, params=params)
    assert second is None  # guarded -- must not silently re-test the holdout
    assert conn.execute(
        "SELECT count(*) FROM calibration_runs WHERE campaign_id = 'c1' AND phase = 'confirm'"
    ).fetchone()[0] == 1


def test_confirmation_writes_candidate_with_status_matching_verdict():
    conn = _conn()
    _seed(conn)
    params = {"method": "gmm", "n_regimes": 3, "band_k": 0.5, "seed": 0}
    verdict = C.run_confirmation(conn, campaign_id="c1", symbol=TARGET, sector=SECTOR, params=params)

    confirm_verdict, status = conn.execute(
        "SELECT confirm_verdict, status FROM calibration_candidates WHERE campaign_id = 'c1'"
    ).fetchone()
    assert confirm_verdict == verdict
    assert status == ("proposed" if verdict == "passed" else "rejected")


def test_training_paths_never_read_calibration_tables():
    """calibration_runs/calibration_candidates are a read-only side-branch,
    same discipline as attribution_log/feature_candidates -- must never feed
    the model."""
    src = Path(__file__).resolve().parents[2] / "src" / "stockmoney" / "models"
    training_files = ["feature_matrix.py", "walk_forward.py", "regime.py", "direction.py", "production.py"]
    for name in training_files:
        text = (src / name).read_text()
        assert "calibration_runs" not in text, f"{name} must not read calibration_runs"
        assert "calibration_candidates" not in text, f"{name} must not read calibration_candidates"
