from __future__ import annotations

from datetime import date, datetime, timezone

import duckdb

from stockmoney.data import attribution as A
from stockmoney.data import trader_predictions as tp
from stockmoney.data import trader_methods as tm
from stockmoney.data.db import run_migrations
from stockmoney.league.review import run_review

# Three 5-trading-day windows (Monday->Monday), used to build several graded
# wrong_signal_existed cases for the method-proposal threshold.
WINDOWS = [
    (date(2026, 6, 1), date(2026, 6, 8)),
    (date(2026, 6, 15), date(2026, 6, 22)),
    (date(2026, 6, 29), date(2026, 7, 6)),
]


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def _put_price(conn, symbol, d, close):
    conn.execute(
        "INSERT INTO ohlcv_daily (symbol, trade_date, close, source, ingested_at) VALUES (?,?,?,?,?)",
        [symbol, d, close, "test", datetime.now(timezone.utc)],
    )


def _seed_flat(conn, start, end):
    """Flat market/sector members over the window -> a SOXL move (SOXL isn't a
    member) is almost entirely idiosyncratic."""
    for m in A.MARKET_MEMBERS:
        _put_price(conn, m, start, 100.0)
        _put_price(conn, m, end, 100.5)


def _record_graded(conn, *, trader_id, direction, start, end, entry=100.0, exit_price=130.0):
    _put_price(conn, "SOXL", start, entry)
    _put_price(conn, "SOXL", end, exit_price)
    pid = tp.record_trader_prediction(
        conn, trader_id=trader_id, method_version=tm.current_method_version(conn, trader_id),
        trade_date=start, symbol="SOXL", sector="semiconductor", horizon=5, label_end_date=end,
        direction=direction, conviction=0.7, rationale="r", invalidation="i", entry_price=entry,
        grade_vol=0.4, engine_payload={}, regime=0,
    )
    tp.grade_trader_prediction(conn, pid, actual_price=exit_price)
    return pid


def test_reused_attribution_verdicts_per_trader():
    conn = _conn()
    start, end = WINDOWS[0]
    _seed_flat(conn, start, end)
    # chartist says up (correct, +30%) ; analyst says down (misses a real move)
    _record_graded(conn, trader_id="chartist", direction="up", start=start, end=end)
    _record_graded(conn, trader_id="analyst", direction="down", start=start, end=end)

    summary = run_review(conn)
    assert summary["reviewed"] == 2
    verdicts = dict(conn.execute(
        "SELECT trader_id, verdict_class FROM trader_review_log"
    ).fetchall())
    assert verdicts["chartist"] == A.RIGHT_REASON_RIGHT
    assert verdicts["analyst"] == A.WRONG_SIGNAL_EXISTED


def test_feature_candidate_only_for_chartist():
    conn = _conn()
    start, end = WINDOWS[0]
    _seed_flat(conn, start, end)
    # both wrong (down) on a +30% move -> both wrong_signal_existed
    _record_graded(conn, trader_id="chartist", direction="down", start=start, end=end)
    _record_graded(conn, trader_id="analyst", direction="down", start=start, end=end)
    run_review(conn)
    names = [r[0] for r in conn.execute("SELECT proposed_feature_name FROM feature_candidates").fetchall()]
    # only the chartist (a model-feature engine) proposes a feature candidate
    assert names == ["idio_signal::SOXL"]


def test_divergence_records_disagreement_and_winner():
    conn = _conn()
    start, end = WINDOWS[0]
    _seed_flat(conn, start, end)
    _record_graded(conn, trader_id="chartist", direction="up", start=start, end=end)
    _record_graded(conn, trader_id="analyst", direction="down", start=start, end=end)
    run_review(conn)
    rows = dict((r[0], (r[1], r[2])) for r in conn.execute(
        "SELECT trader_id, disagreed, was_right FROM trader_divergence_log"
    ).fetchall())
    assert rows["chartist"] == (True, True)   # disagreed, and was right
    assert rows["analyst"] == (True, False)   # disagreed, and was wrong


def test_review_is_idempotent():
    conn = _conn()
    start, end = WINDOWS[0]
    _seed_flat(conn, start, end)
    _record_graded(conn, trader_id="chartist", direction="up", start=start, end=end)
    run_review(conn)
    second = run_review(conn)
    assert second["reviewed"] == 0
    assert conn.execute("SELECT count(*) FROM trader_review_log").fetchone()[0] == 1


def test_method_proposal_after_repeated_wrong_signal_existed():
    conn = _conn()
    # three graded chartist wrong_signal_existed cases -> one method proposal
    for start, end in WINDOWS:
        _seed_flat(conn, start, end)
        _record_graded(conn, trader_id="chartist", direction="down", start=start, end=end)
    summary = run_review(conn)
    assert summary["method_proposals"] == 1
    proposals = tm.list_proposals(conn, status="proposed")
    assert len(proposals) == 1
    assert proposals[0]["trader_id"] == "chartist"
    assert proposals[0]["from_version"] == "chartist:gmm-logistic-v2"
