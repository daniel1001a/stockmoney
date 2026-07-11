"""Weekend calibration campaign (~/.claude/plans/frolicking-wishing-cook.md):
systematic walk-forward hyperparameter search over CLAUDE.md section 16's
"not yet optimized" parameters, with an explicit held-out confirmation window
so "improved" never means "won a lottery over N grid cells on the same
history" (CLAUDE.md section 12's ban on picking a winner after a full-sample
backtest).

Three tiers, cheapest last:
  Tier 1 (expensive -- refits regime+direction models): sweep {method,
    n_regimes, band_k, seed} per watchlist symbol via
    stockmoney.models.walk_forward.run_walk_forward, the exact same machinery
    module A's own backtests already use.
  Tier 2/3 (cheap -- post-hoc over an already-computed WalkForwardResult, no
    refit): EV gate window/quantile (ev_gate.run_ev_gate) and Kelly
    conservative_coeff (kelly.position_size), applied only to the Tier-1
    shortlist, informational only.

Search vs confirm: the dataset's most recent HOLDOUT_FRAC of rows is
truncated out entirely before the search grid ever runs (see
_truncate_dataset) -- Tier 1 search cannot see it no matter how many configs
are tried, and re-running search can never leak it either since it was never
loaded. Only the small post-search shortlist gets a *single* confirmation run
against the untouched holdout tail (n_folds=1 spanning exactly that tail),
written with phase='confirm' and guarded by _already_ran to never re-run for
the same (campaign_id, symbol, phase, params) key -- re-testing against the
same holdout after seeing it fail would defeat the entire point of holding it
out.

A candidate "passes" only if, using ITS OWN regime labels to define the
comparison segments (not the baseline's -- two models with different
n_regimes have no natural regime-to-regime correspondence), at least one
regime shows a bootstrap-significant improvement in *Brier skill over
climatology* against the fixed production baseline (gmm, n_regimes=3,
band_k=0.5, seed=0) restricted to the same trade_dates, and no regime shows a
significant *worsening* -- CLAUDE.md section 12's "never merge regimes into
one number" enforced as a hard gate, not just a reporting habit.

The metric is skill over climatology, NOT raw Brier, on purpose: band_k is a
label-definition knob, and a wider band mechanically lowers the raw-Brier
floor (more days fall in the dominant RANGE class, which a model can score
well on by just predicting the majority class). Comparing raw Brier across
band_k values therefore rewards the widest band rather than the best model --
an apples-to-oranges comparison of different-difficulty scoring targets.
Normalising each config against its own base-rate (climatology) forecast
removes that confound; see _climatology_skill_per_sample.

Residual caveat (documented, not silently ignored): confirming the top-K
shortlist per symbol against the holdout is itself many hypothesis tests at
alpha=0.05, so some false positives are expected there too. The holdout
defeats the winner's-curse point-estimate bias of search, not the
multiplicity of the confirm step. The seed sweep is the practical mitigation
-- an edge that replicates across seeds is far less likely to be a fluke --
and every survivor is only ever *proposed* for human review, never
auto-promoted.

This module and its CLI (scripts/run_calibration_campaign.py) never write to
production.py / ev_gate.py / kelly.py's defaults, or to any table a training
path reads -- calibration_runs/calibration_candidates are the same read-only,
human-gated "propose, don't promote" side-branch as attribution.py's
feature_candidates.
"""
from __future__ import annotations

import hashlib
import json
import statistics
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone

import duckdb
import numpy as np

from stockmoney.data.watchlist import sector_for_symbol
from stockmoney.models.direction import LogisticDirectionModel
from stockmoney.models.ev_gate import (
    DEFAULT_COST_BPS,
    MIN_RESOLVED_TRADES,
    derive_trade_outcomes,
    expanding_stats_asof,
    run_ev_gate,
)
from stockmoney.models.feature_matrix import Dataset, build_feature_matrix, to_dataset
from stockmoney.models.kelly import position_size
from stockmoney.models.metrics import bootstrap_paired_diff_ci, brier_per_sample, report_by_regime
from stockmoney.models.regime import HMMTrack, KMeansGMMTrack
from stockmoney.models.walk_forward import WalkForwardResult, run_walk_forward

HORIZON = 5
COST_BPS = DEFAULT_COST_BPS

# Production baseline: whatever stockmoney.models.production actually runs
# today (gmm-logistic-v2). Every candidate is judged against this fixed
# reference, never against other candidates -- comparing candidates pairwise
# across a large grid would multiply the multiple-comparison problem for no
# decision-relevant reason (the only question that matters is "should we
# replace what's running today").
BASELINE_METHOD = "gmm"
BASELINE_N_REGIMES = 3
BASELINE_BAND_K = 0.5
BASELINE_SEED = 0

GRID_METHODS = ["gmm", "hmm"]
GRID_N_REGIMES = [2, 3, 4, 5]
GRID_BAND_K = [0.3, 0.4, 0.5, 0.6, 0.7]
# seed varies ONLY the regime track's stochastic fit (GMM/HMM init); the
# direction model is always LogisticDirectionModel(seed=0). Sweeping it is a
# robustness probe -- a config whose edge only shows up on one seed is noise,
# one that holds across seeds is a real signal (this is what makes the
# multiple-comparison exposure of the confirm step tolerable: replicated-
# across-seeds survivors are far less likely to be flukes).
GRID_SEEDS = [0, 1, 2, 3, 4]

HOLDOUT_FRAC = 0.15  # most recent slice of history, structurally invisible to search
SEARCH_N_FOLDS = 4
SEARCH_INITIAL_FRAC = 0.5
EMBARGO = 5

SHORTLIST_TOP_K = 3

EV_GRID_WINDOW = [30, 60, 90, 120]
EV_GRID_QUANTILE = [0.5, 0.6, 0.75, 0.85]
KELLY_GRID_COEFF = [0.25, 0.35, 0.5]


def active_watchlist_symbols(conn: duckdb.DuckDBPyConnection) -> list[tuple[str, str]]:
    """(symbol, sector) pairs for every active watchlist member -- same query
    daily_prediction_cli.py's record-watchlist uses, sector resolved via
    stockmoney.data.watchlist.sector_for_symbol."""
    rows = conn.execute(
        "SELECT symbol FROM watchlist_members WHERE removed_date IS NULL ORDER BY symbol"
    ).fetchall()
    out = []
    for (symbol,) in rows:
        sector = sector_for_symbol(conn, symbol)
        if sector is not None:
            out.append((symbol, sector))
    return out


def _truncate_dataset(dataset: Dataset, n_rows: int) -> Dataset:
    """Drop everything from n_rows onward -- makes the confirmation holdout
    tail structurally invisible to the search grid, not just "unlikely to be
    picked." No amount of re-running search can touch these rows because
    they were never loaded into its Dataset at all."""
    return Dataset(
        X=dataset.X[:n_rows],
        regime_X=dataset.regime_X[:n_rows],
        y=dataset.y[:n_rows],
        trade_dates=dataset.trade_dates[:n_rows],
        available_at=dataset.available_at[:n_rows],
        label_end_dates=dataset.label_end_dates[:n_rows],
        fwd_return=dataset.fwd_return[:n_rows],
    )


def _make_track(method: str, n_regimes: int, seed: int):
    if method == "gmm":
        return KMeansGMMTrack(method="gmm", n_regimes=n_regimes, seed=seed)
    if method == "hmm":
        return HMMTrack(n_regimes=n_regimes, seed=seed)
    raise ValueError(f"unknown method: {method!r}")


def _run_id(campaign_id: str, symbol: str, phase: str, params: dict) -> str:
    """Deterministic per (campaign_id, symbol, phase, params) -- re-running
    the same configuration reproduces the same run_id, so _already_ran can
    skip recomputation on resume rather than just dedupe at insert time."""
    key = json.dumps(
        {"campaign_id": campaign_id, "symbol": symbol, "phase": phase, "params": params}, sort_keys=True
    )
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _already_ran(conn: duckdb.DuckDBPyConnection, run_id: str) -> bool:
    return conn.execute("SELECT 1 FROM calibration_runs WHERE run_id = ?", [run_id]).fetchone() is not None


@dataclass
class RegimeVerdict:
    regime: int
    n: int
    skill_diff: float  # candidate skill-over-climatology MINUS baseline's, in Brier units; positive = candidate more skilful
    ci_lo: float
    ci_hi: float
    improved: bool
    worsened: bool


@dataclass
class CriteriaVerdict:
    passes: bool
    per_regime: list[RegimeVerdict]
    overall_skill_diff: float  # same measure pooled over all OOS rows -- the n-weighted ranking metric


def _climatology_skill_per_sample(result: WalkForwardResult) -> np.ndarray:
    """Per-sample Brier *skill over climatology*: how much the model beats a
    constant base-rate forecast (predict this config's own empirical class
    frequencies every day). Returns clim_brier_i - model_brier_i, so higher =
    more skill.

    This is the fix for the label-difficulty confound: a wider band_k pushes
    more days into the dominant RANGE class, mechanically lowering the raw
    Brier floor (you can score well by confidently predicting the majority
    class), which made cross-band_k raw-Brier comparison reward the widest
    band rather than the best model. Normalising each config against ITS OWN
    climatology cancels that -- a config only shows skill by beating the
    base rate its own label definition implies. The climatology is an
    evaluation-metric reference computed on the OOS sample's class
    frequencies; it is never a traded forecast and never re-enters training,
    so there is no look-ahead consequence to the model itself (same read-only
    side-branch discipline as the rest of this module).
    """
    y = result.y_true
    freqs = np.bincount(y, minlength=3) / len(y)
    clim_proba = np.tile(freqs, (len(y), 1))
    return brier_per_sample(y, clim_proba) - brier_per_sample(y, result.proba)


def compare_to_baseline(candidate: WalkForwardResult, baseline: WalkForwardResult) -> CriteriaVerdict:
    """Per-regime bootstrap comparison of Brier *skill over climatology*
    (see _climatology_skill_per_sample), segmented by the CANDIDATE's own
    regime labels (not baseline's -- a candidate with n_regimes=4 has no
    natural correspondence to baseline's 3 regimes, so "regime 0" only means
    something in the context of whichever model produced it). Baseline's
    per-sample skill is matched to the candidate's OOS rows by trade_date:
    both runs see an identical row set for a fixed symbol/horizon/n_folds/
    initial_frac/embargo (band_k only changes labels, never which rows
    exist), so this join always fully resolves -- a KeyError here would mean
    the two runs used different walk-forward configs, a real bug worth
    surfacing loudly rather than swallowing.

    Passes iff at least one regime shows a significantly higher skill than
    the production baseline and no regime shows a significantly lower one.
    `overall_skill_diff` pools every OOS row (so it is inherently sample-size
    weighted, unlike a single per-regime figure) and is what shortlisting
    ranks on -- deliberately not the single best regime, which would reward a
    small-n regime's noisy fluke.
    """
    baseline_skill_by_date = dict(
        zip(baseline.trade_dates, _climatology_skill_per_sample(baseline))
    )
    cand_skill = _climatology_skill_per_sample(candidate)

    per_regime: list[RegimeVerdict] = []
    for r in sorted(set(candidate.regime.tolist())):
        mask = candidate.regime == r
        dates_r = [d for d, m in zip(candidate.trade_dates, mask) if m]
        cand_r = cand_skill[mask]
        base_r = np.array([baseline_skill_by_date[d] for d in dates_r])
        # skill_diff = candidate skill - baseline skill; positive = candidate better,
        # so the CI being strictly above 0 means a significant improvement.
        mean_diff, lo, hi = bootstrap_paired_diff_ci(cand_r, base_r)
        per_regime.append(
            RegimeVerdict(
                regime=int(r), n=len(dates_r), skill_diff=mean_diff, ci_lo=lo, ci_hi=hi,
                improved=lo > 0, worsened=hi < 0,
            )
        )

    base_all = np.array([baseline_skill_by_date[d] for d in candidate.trade_dates])
    overall_diff, _, _ = bootstrap_paired_diff_ci(cand_skill, base_all)

    any_improved = any(v.improved for v in per_regime)
    any_worsened = any(v.worsened for v in per_regime)
    return CriteriaVerdict(
        passes=any_improved and not any_worsened,
        per_regime=per_regime,
        overall_skill_diff=overall_diff,
    )


def _write_run(
    conn: duckdb.DuckDBPyConnection, *, run_id: str, campaign_id: str, symbol: str, sector: str,
    phase: str, param_family: str, params: dict, n_folds: int, initial_frac: float, embargo: int,
    seed: int, report: dict, verdict: CriteriaVerdict, started_at: datetime, finished_at: datetime,
) -> None:
    overall = report["overall"]
    per_regime_json = json.dumps(
        [{"regime": r.regime, "n": r.n, "accuracy": r.accuracy, "brier": r.brier, "sharpe": r.sharpe}
         for r in report["per_regime"]]
    )
    vs_baseline_json = json.dumps({
        "overall_skill_diff": verdict.overall_skill_diff,
        "per_regime": [
            {"regime": v.regime, "n": v.n, "skill_diff": v.skill_diff, "ci_lo": v.ci_lo, "ci_hi": v.ci_hi,
             "improved": v.improved, "worsened": v.worsened}
            for v in verdict.per_regime
        ],
    })
    conn.execute(
        """
        INSERT INTO calibration_runs (
            run_id, campaign_id, symbol, sector, phase, param_family, params,
            n_folds, initial_frac, embargo, seed,
            overall_accuracy, overall_brier, overall_sharpe, baseline_accuracy,
            per_regime_json, vs_baseline_json, n_obs, criteria_passes,
            started_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            run_id, campaign_id, symbol, sector, phase, param_family, json.dumps(params),
            n_folds, initial_frac, embargo, seed,
            overall.accuracy, overall.brier, overall.sharpe, report["baseline_accuracy"],
            per_regime_json, vs_baseline_json, overall.n, verdict.passes,
            started_at, finished_at,
        ],
    )


def run_search_grid(
    conn: duckdb.DuckDBPyConnection, *, campaign_id: str, symbols: list[tuple[str, str]] | None = None,
) -> dict[str, int]:
    """Tier 1: sweep {method, n_regimes, band_k, seed} per symbol, writing
    every result (not just winners) to calibration_runs with phase='search'.
    Idempotent and resumable per (campaign_id, symbol, params) -- safe to
    interrupt and re-invoke."""
    symbols = symbols if symbols is not None else active_watchlist_symbols(conn)
    counts = {"written": 0, "skipped_existing": 0}

    for symbol, sector in symbols:
        baseline_matrix = build_feature_matrix(
            conn, target_symbol=symbol, sector=sector, horizon=HORIZON, band_k=BASELINE_BAND_K
        )
        baseline_full = to_dataset(baseline_matrix)
        n_total = len(baseline_full.trade_dates)
        holdout_start = int(n_total * (1 - HOLDOUT_FRAC))
        baseline_search_ds = _truncate_dataset(baseline_full, holdout_start)
        baseline_result = run_walk_forward(
            baseline_search_ds,
            lambda: _make_track(BASELINE_METHOD, BASELINE_N_REGIMES, BASELINE_SEED),
            lambda: LogisticDirectionModel(seed=0, calibrate="sigmoid"),
            n_folds=SEARCH_N_FOLDS, initial_frac=SEARCH_INITIAL_FRAC, embargo=EMBARGO,
        )

        for band_k in GRID_BAND_K:
            matrix = build_feature_matrix(
                conn, target_symbol=symbol, sector=sector, horizon=HORIZON, band_k=band_k
            )
            full_ds = to_dataset(matrix)
            search_ds = _truncate_dataset(full_ds, holdout_start)

            for method in GRID_METHODS:
                for n_regimes in GRID_N_REGIMES:
                    for seed in GRID_SEEDS:
                        params = {"method": method, "n_regimes": n_regimes, "band_k": band_k, "seed": seed}
                        run_id = _run_id(campaign_id, symbol, "search", params)
                        if _already_ran(conn, run_id):
                            counts["skipped_existing"] += 1
                            continue

                        started = datetime.now(timezone.utc)
                        result = run_walk_forward(
                            search_ds,
                            lambda m=method, n=n_regimes, s=seed: _make_track(m, n, s),
                            lambda: LogisticDirectionModel(seed=0, calibrate="sigmoid"),
                            n_folds=SEARCH_N_FOLDS, initial_frac=SEARCH_INITIAL_FRAC, embargo=EMBARGO,
                        )
                        report = report_by_regime(result, horizon=HORIZON, cost_bps=COST_BPS)
                        verdict = compare_to_baseline(result, baseline_result)
                        finished = datetime.now(timezone.utc)

                        _write_run(
                            conn, run_id=run_id, campaign_id=campaign_id, symbol=symbol, sector=sector,
                            phase="search", param_family="regime", params=params,
                            n_folds=SEARCH_N_FOLDS, initial_frac=SEARCH_INITIAL_FRAC, embargo=EMBARGO,
                            seed=seed, report=report, verdict=verdict,
                            started_at=started, finished_at=finished,
                        )
                        counts["written"] += 1
    return counts


def shortlist_top_k(
    conn: duckdb.DuckDBPyConnection, *, campaign_id: str, symbol: str, top_k: int = SHORTLIST_TOP_K
) -> list[dict]:
    """Rank a symbol's phase='search' rows that passed criteria by their
    pooled, sample-size-weighted `overall_skill_diff` (most positive = most
    skilful over the production baseline), return the top_k params dicts.
    Ranking on the pooled figure rather than a single best per-regime number
    deliberately avoids rewarding a small-n regime's noisy fluke. A symbol
    with zero passing rows returns an empty list -- a common, expected,
    non-embarrassing outcome given the project's already-documented weak
    baseline signal, not a bug to work around."""
    rows = conn.execute(
        """
        SELECT params, vs_baseline_json FROM calibration_runs
        WHERE campaign_id = ? AND symbol = ? AND phase = 'search' AND criteria_passes = TRUE
        """,
        [campaign_id, symbol],
    ).fetchall()
    scored = []
    for params_json, vs_baseline_json in rows:
        overall_skill_diff = json.loads(vs_baseline_json)["overall_skill_diff"]
        scored.append((overall_skill_diff, json.loads(params_json)))
    scored.sort(key=lambda t: -t[0])  # most positive skill advantage first
    return [params for _, params in scored[:top_k]]


def _win_rate(pnls: list[float]) -> float | None:
    if not pnls:
        return None
    return sum(1 for p in pnls if p > 0) / len(pnls)


def _avg(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def run_ev_kelly_grid(conn: duckdb.DuckDBPyConnection, *, symbol: str, sector: str, params: dict) -> dict:
    """Tier 2/3: cheap post-hoc EV-gate/Kelly grid over one shortlisted
    config's own search-window walk-forward outcomes (no refit).
    Informational only -- CLAUDE.md section 16's EV-gate/Kelly parameters are
    risk-management dials, not something with a single statistically
    'correct' answer the way Brier calibration is. This surfaces which
    (window, quantile) most cleanly separates winning from losing trades,
    using the same passed-vs-blocked comparison
    stockmoney.models.backtest_ev_gate.py already established; Kelly's
    conservative_coeff has no 'winner' to pick (it's the user's risk
    tolerance dial, CLAUDE.md section 7), so all three grid values are just
    reported side by side.
    """
    matrix = build_feature_matrix(
        conn, target_symbol=symbol, sector=sector, horizon=HORIZON, band_k=params["band_k"]
    )
    dataset = to_dataset(matrix)
    n_total = len(dataset.trade_dates)
    holdout_start = int(n_total * (1 - HOLDOUT_FRAC))
    search_ds = _truncate_dataset(dataset, holdout_start)

    result = run_walk_forward(
        search_ds,
        lambda: _make_track(params["method"], params["n_regimes"], params["seed"]),
        lambda: LogisticDirectionModel(seed=0, calibrate="sigmoid"),
        n_folds=SEARCH_N_FOLDS, initial_frac=SEARCH_INITIAL_FRAC, embargo=EMBARGO,
    )
    outcomes = derive_trade_outcomes(result)

    ev_grid = []
    for window in EV_GRID_WINDOW:
        for quantile in EV_GRID_QUANTILE:
            gate_results = run_ev_gate(
                outcomes, window=window, quantile=quantile, cost_bps=COST_BPS, min_trades=MIN_RESOLVED_TRADES
            )
            gated = [(o, r) for o, r in zip(outcomes, gate_results) if r.threshold is not None]
            passed = [o.pnl_gross for o, r in gated if r.tradeable]
            blocked = [o.pnl_gross for o, r in gated if not r.tradeable]
            ev_grid.append({
                "window": window, "quantile": quantile,
                "passed_n": len(passed), "passed_win_rate": _win_rate(passed), "passed_avg_pnl": _avg(passed),
                "blocked_n": len(blocked), "blocked_win_rate": _win_rate(blocked), "blocked_avg_pnl": _avg(blocked),
            })

    kelly_grid = []
    for coeff in KELLY_GRID_COEFF:
        sizes = []
        for o in outcomes:
            stats = expanding_stats_asof(outcomes, o.trade_date, min_trades=MIN_RESOLVED_TRADES)
            if stats is None:
                continue
            sizes.append(position_size(stats.p_win, stats.avg_win, stats.avg_loss, conservative_coeff=coeff))
        nonzero = [s for s in sizes if s > 0]
        kelly_grid.append({
            "conservative_coeff": coeff, "n": len(sizes), "n_nonzero": len(nonzero),
            "mean_size": _avg(nonzero), "median_size": _median(nonzero),
            "max_size": max(nonzero) if nonzero else None,
        })

    return {"ev_grid": ev_grid, "kelly_grid": kelly_grid}


def _write_candidate(
    conn: duckdb.DuckDBPyConnection, *, campaign_id: str, symbol: str, params: dict,
    confirm_verdict: str, ev_kelly: dict | None,
) -> str:
    candidate_id = str(uuid.uuid4())
    status = "proposed" if confirm_verdict == "passed" else "rejected"
    conn.execute(
        """
        INSERT INTO calibration_candidates (
            candidate_id, campaign_id, symbol, params, search_verdict, confirm_verdict,
            ev_kelly_json, status, proposed_date, created_at
        ) VALUES (?, ?, ?, ?, 'passed', ?, ?, ?, ?, ?)
        """,
        [
            candidate_id, campaign_id, symbol, json.dumps(params), confirm_verdict,
            json.dumps(ev_kelly) if ev_kelly else None, status, date.today(), datetime.now(timezone.utc),
        ],
    )
    return candidate_id


def run_confirmation(
    conn: duckdb.DuckDBPyConnection, *, campaign_id: str, symbol: str, sector: str, params: dict,
) -> str | None:
    """Single, non-repeatable check of one shortlisted config against the
    untouched holdout tail. Returns 'passed'/'failed', or None if this
    (campaign_id, symbol, params) combo was already confirmed -- confirmation
    is designed to never silently re-run, since re-testing against the same
    holdout after seeing it fail would defeat the entire reason it's held
    out. On pass, also runs the Tier 2/3 EV/Kelly grid and attaches it;
    writes a calibration_candidates row either way (status='proposed' on
    pass, auto 'rejected' on fail -- a holdout failure needs no human review
    queue time)."""
    run_id = _run_id(campaign_id, symbol, "confirm", params)
    if _already_ran(conn, run_id):
        return None

    baseline_matrix = build_feature_matrix(
        conn, target_symbol=symbol, sector=sector, horizon=HORIZON, band_k=BASELINE_BAND_K
    )
    baseline_full = to_dataset(baseline_matrix)
    n_total = len(baseline_full.trade_dates)
    holdout_start = int(n_total * (1 - HOLDOUT_FRAC))
    confirm_initial_frac = holdout_start / n_total

    baseline_result = run_walk_forward(
        baseline_full,
        lambda: _make_track(BASELINE_METHOD, BASELINE_N_REGIMES, BASELINE_SEED),
        lambda: LogisticDirectionModel(seed=0, calibrate="sigmoid"),
        n_folds=1, initial_frac=confirm_initial_frac, embargo=EMBARGO,
    )

    matrix = build_feature_matrix(
        conn, target_symbol=symbol, sector=sector, horizon=HORIZON, band_k=params["band_k"]
    )
    full_ds = to_dataset(matrix)
    started = datetime.now(timezone.utc)
    result = run_walk_forward(
        full_ds,
        lambda: _make_track(params["method"], params["n_regimes"], params["seed"]),
        lambda: LogisticDirectionModel(seed=0, calibrate="sigmoid"),
        n_folds=1, initial_frac=confirm_initial_frac, embargo=EMBARGO,
    )
    report = report_by_regime(result, horizon=HORIZON, cost_bps=COST_BPS)
    verdict = compare_to_baseline(result, baseline_result)
    finished = datetime.now(timezone.utc)

    verdict_str = "passed" if verdict.passes else "failed"
    # Compute the (read-only) EV/Kelly grid before opening the write
    # transaction, so the two writes below are the only statements inside it.
    ev_kelly = run_ev_kelly_grid(conn, symbol=symbol, sector=sector, params=params) if verdict.passes else None

    # Atomic: the confirm run row and its candidate row must land together.
    # Otherwise a crash between them would leave a confirmed run with no
    # candidate, and _already_ran's idempotency guard would then block any
    # re-run that could repair it (a holdout is a one-shot resource).
    conn.execute("BEGIN")
    try:
        _write_run(
            conn, run_id=run_id, campaign_id=campaign_id, symbol=symbol, sector=sector,
            phase="confirm", param_family="regime", params=params,
            n_folds=1, initial_frac=confirm_initial_frac, embargo=EMBARGO, seed=params["seed"],
            report=report, verdict=verdict, started_at=started, finished_at=finished,
        )
        _write_candidate(
            conn, campaign_id=campaign_id, symbol=symbol, params=params,
            confirm_verdict=verdict_str, ev_kelly=ev_kelly,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return verdict_str


def new_campaign_id() -> str:
    return f"weekend-{date.today().isoformat()}-{uuid.uuid4().hex[:8]}"
