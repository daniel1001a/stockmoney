import json
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from stockmoney.data.db import run_migrations
import calibration_report_query as Q

NOW = datetime.now(timezone.utc)


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def _insert_run(conn, *, campaign_id, symbol, phase, params, criteria_passes):
    conn.execute(
        """
        INSERT INTO calibration_runs (
            run_id, campaign_id, symbol, sector, phase, param_family, params,
            n_folds, initial_frac, embargo, seed,
            overall_accuracy, overall_brier, overall_sharpe, baseline_accuracy,
            per_regime_json, vs_baseline_json, n_obs, criteria_passes,
            started_at, finished_at
        ) VALUES (?, ?, ?, 'semiconductor', ?, 'regime', ?, 4, 0.5, 5, ?,
            0.4, 0.6, 0.1, 0.38, '[]', '{}', 100, ?, ?, ?)
        """,
        [
            str(uuid.uuid4()), campaign_id, symbol, phase, json.dumps(params), params["seed"],
            criteria_passes, NOW, NOW,
        ],
    )


def _insert_candidate(conn, *, campaign_id, symbol, params, ev_grid):
    conn.execute(
        """
        INSERT INTO calibration_candidates (
            candidate_id, campaign_id, symbol, params, search_verdict, confirm_verdict,
            ev_kelly_json, status, proposed_date, created_at
        ) VALUES (?, ?, ?, ?, 'passed', 'passed', ?, 'proposed', ?, ?)
        """,
        [
            str(uuid.uuid4()), campaign_id, symbol, json.dumps(params),
            json.dumps({"ev_grid": ev_grid, "kelly_grid": []}), date.today(), NOW,
        ],
    )


def test_no_campaign_yet_reports_null_campaign_id():
    conn = _conn()
    report = Q.build_report(conn)
    assert report["campaign_id"] is None


def test_seed_robustness_reflects_how_many_of_the_seeds_passed():
    conn = _conn()
    params_base = {"method": "hmm", "n_regimes": 4, "band_k": 0.7}
    # 5 seeds tried for this exact (method, n_regimes, band_k); only seed 3 passed.
    for seed in range(5):
        _insert_run(
            conn, campaign_id="c1", symbol="SOXL", phase="search",
            params={**params_base, "seed": seed}, criteria_passes=(seed == 3),
        )
    _insert_candidate(
        conn, campaign_id="c1", symbol="SOXL", params={**params_base, "seed": 3}, ev_grid=[],
    )

    report = Q.build_report(conn)
    entry = report["proposed_candidates_awaiting_review"][0]
    assert entry["seed_robustness"] == {"seeds_tried": 5, "seeds_passed": 1}


def test_best_ev_gate_omitted_when_no_window_ever_let_a_trade_through():
    conn = _conn()
    params = {"method": "gmm", "n_regimes": 3, "band_k": 0.5, "seed": 0}
    _insert_run(conn, campaign_id="c1", symbol="SOXL", phase="search", params=params, criteria_passes=True)
    # Every grid cell has passed_n == 0 -- the gate never let a trade through.
    ev_grid = [
        {"window": w, "quantile": q, "passed_n": 0, "passed_win_rate": None, "passed_avg_pnl": None,
         "blocked_n": 5, "blocked_win_rate": 0.2, "blocked_avg_pnl": -0.01}
        for w in (30, 60) for q in (0.5, 0.75)
    ]
    _insert_candidate(conn, campaign_id="c1", symbol="SOXL", params=params, ev_grid=ev_grid)

    report = Q.build_report(conn)
    entry = report["proposed_candidates_awaiting_review"][0]
    assert "best_ev_gate" not in entry


def test_best_ev_gate_present_when_a_window_did_let_trades_through():
    conn = _conn()
    params = {"method": "gmm", "n_regimes": 3, "band_k": 0.5, "seed": 0}
    _insert_run(conn, campaign_id="c1", symbol="SOXL", phase="search", params=params, criteria_passes=True)
    ev_grid = [
        {"window": 30, "quantile": 0.5, "passed_n": 0, "passed_win_rate": None, "passed_avg_pnl": None,
         "blocked_n": 5, "blocked_win_rate": 0.2, "blocked_avg_pnl": -0.01},
        {"window": 90, "quantile": 0.75, "passed_n": 12, "passed_win_rate": 0.6, "passed_avg_pnl": 0.02,
         "blocked_n": 20, "blocked_win_rate": 0.3, "blocked_avg_pnl": -0.005},
    ]
    _insert_candidate(conn, campaign_id="c1", symbol="SOXL", params=params, ev_grid=ev_grid)

    report = Q.build_report(conn)
    entry = report["proposed_candidates_awaiting_review"][0]
    assert entry["best_ev_gate"] == {"window": 90, "quantile": 0.75, "passed_win_rate": 0.6, "passed_n": 12}
