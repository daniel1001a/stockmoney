"""Unified, costed replay evidence. No real broker, mail, or data download.

Results are frozen by semantic variant, cost assumptions, sleeve and window.
Synthetic option quotes remain explicitly weaker than forward paper evidence.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from stockmoney.desk import backtest
from stockmoney.desk.calendar import is_trading_day
from stockmoney.desk.options.config import GROK, OptionsConfig
from stockmoney.desk.strategies import REGISTRY

OPTION_PARAMS = {
    "gap_min_pct_index", "gap_min_pct_stock", "gap_go_min_rvol", "orb_break_volume_mult",
    "orb_min_day_rvol", "vwap_hold_bars", "vwap_slope_bars", "vwap_slope_min",
}
SCHEMA = """CREATE TABLE IF NOT EXISTS replay_results (
    variant_hash TEXT, start TEXT, end TEXT, result_json TEXT NOT NULL,
    PRIMARY KEY(variant_hash,start,end));"""


def _variant(sleeve, variant):
    if variant == "incumbent":
        return {}, OptionsConfig(), {"variant": "incumbent"}, False
    if not isinstance(variant, dict) or variant.get("sleeve") != sleeve or variant.get("kind") != "param":
        raise ValueError("variant must be incumbent or a single-parameter proposal for this sleeve")
    change = variant.get("param")
    if not isinstance(change, dict) or set(change) != {"key", "new"} or variant.get("filter") is not None:
        raise ValueError("variant must contain exactly one parameter change")
    key, value = change["key"], change["new"]
    if not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("invalid variant key or value")
    semantic = {"key": key, "new": value}
    if GROK in key or "in_play" in key or key == "options.grok_min_rvol":
        return {}, OptionsConfig(), semantic, True
    if sleeve == "stocks":
        pieces = key.split(".")
        if len(pieces) != 2 or pieces[0] not in REGISTRY or pieces[1] not in REGISTRY[pieces[0]].bounds:
            raise ValueError("unknown strategy parameter")
        name, param = pieces
        params = REGISTRY[name].validate_params({param: value})
        semantic["new"] = params[param]
        return {name: params}, OptionsConfig(), semantic, False
    if sleeve == "options" and key.startswith("options.") and key[8:] in OPTION_PARAMS:
        attr = key[8:]
        default = getattr(OptionsConfig(), attr)
        if value <= 0 or (isinstance(default, int) and value != int(value)):
            raise ValueError("option threshold must be positive, and whole for bar counts")
        value = int(value) if isinstance(default, int) else float(value)
        semantic["new"] = value
        return {}, replace(OptionsConfig(), **{attr: value}), semantic, False
    raise ValueError("unknown or protected replay parameter")


def _drawdown(equity):
    peak, dd = 25_000.0, 0.0
    for value in equity:
        peak = max(peak, value)
        dd = max(dd, (peak - value) / peak)
    return dd


def _result(sleeve, start, end, digest, trades, equity, caveats, costs, decisions=(), status="ok",
            missing_cache_dates=()):
    rs = [t["r_multiple"] for t in trades]
    if any(not math.isfinite(r) for r in rs):
        raise ValueError("non-finite replay return")
    return {"tier": "replay", "sleeve": sleeve, "from": start, "to": end, "variant_hash": digest,
            "status": status, "trades": trades, "n": len(trades),
            "avg_r": sum(rs) / len(rs) if rs else None,
            "win_rate": sum(t["pnl"] > 0 for t in trades) / len(trades) if trades else None,
            "max_drawdown": _drawdown(equity), "caveats": sorted(set(caveats)), "costs": costs,
            "decisions": list(decisions), "missing_cache_dates": list(missing_cache_dates)}


def _options(start, end, cfg, cache_dir, feed):
    from stockmoney.desk.options import replay as options_replay
    trades, decisions, equity = [], [], [25_000.0]
    cumulative = 0.0
    with TemporaryDirectory(prefix="stockmoney-replay-") as work:
        day = date.fromisoformat(start)
        while day <= date.fromisoformat(end):
            if is_trading_day(day):
                result = options_replay.run(day, cfg, cache_dir, Path(work), feed=feed)
                equity.extend(value + cumulative for value in result.shadow_equity)
                with sqlite3.connect(result.db_path) as con:
                    con.row_factory = sqlite3.Row
                    # Exactly one DTE/exit arm per rule, never duplicate its variants.
                    rows = con.execute("SELECT * FROM options_positions WHERE mode='shadow' AND state='EXITED' "
                                       "AND dte_label=? AND exit_variant=? AND source NOT IN (?, 'orb15_5m') "
                                       "ORDER BY exit_time, position_id", (cfg.live_dte, cfg.live_exit, GROK)).fetchall()
                    for row in rows:
                        risk = row["entry_price"] * row["qty_initial"] * cfg.contract_multiplier
                        pnl = row["realized_usd"] - row["fees_usd"]
                        trades.append({"symbol": row["underlying"], "strategy": row["source"],
                                       "entry_date": row["entry_time"], "exit_date": row["exit_time"],
                                       "pnl": pnl, "r_multiple": pnl / risk, "fees": row["fees_usd"]})
                    for row in con.execute("SELECT setup_id,underlying,source,signal_json FROM options_setups "
                                           "WHERE signal_json IS NOT NULL AND source!=? ORDER BY setup_id", (GROK,)):
                        decisions.append(dict(row))
                cumulative = sum(t["pnl"] for t in trades)
                equity.append(25_000.0 + cumulative)
            day += timedelta(days=1)
    trades.sort(key=lambda t: (t["exit_date"], t["symbol"], t["strategy"]))
    return trades, equity, decisions


SPREAD_EXCLUDED = ("expiry_late", "expiry_unpriced")  # excluded from the H6 statistics, as in spreads.report


def _spread_slots(day):
    """The live schedule: every 10 minutes from 09:45 ET (scripts/desk_job.sh),
    only while the session is open (spreads.main returns outside regular hours,
    which end at 13:00 on an early close)."""
    from stockmoney.desk.calendar import close_time
    from stockmoney.desk.options import spreads
    t = datetime.combine(day, time(9, 45), spreads.ET)
    close = datetime.combine(day, time(*close_time(day)), spreads.ET)
    while t.time() <= time(15, 55) and t < close:
        yield t
        t += timedelta(minutes=10)


def _bars_end(market):
    """When the day's stock minute bars stop (last bar start + 1 minute), or None."""
    import pandas as pd
    ends = [b.index[-1] for b in market.stock.values() if len(b)]
    return (max(ends) + pd.Timedelta(minutes=1)).to_pydatetime() if ends else None


def _wind_states(bars, start, end):
    """{session: wind state} from causal daily indicators (row t uses rows <= t),
    for every session from a little before ``start`` (state_before reads the
    last session strictly before the replayed day) through ``end``."""
    import pandas as pd

    from stockmoney.desk import indicators, wind
    ind = indicators.compute(bars.upto(pd.Timestamp(end)))
    frame = wind.series(ind, str(_spread_warmup(start) - timedelta(days=10)), end)
    return {} if frame.empty else {str(d): str(s) for d, s in frame["state"].items()}


def _spread_warmup(start):
    """First day ticked: a spread entered up to DTE_MAX calendar days before
    ``start`` can still be open on it, so the book is rebuilt from there."""
    from stockmoney.desk.options import spreads
    return date.fromisoformat(start) - timedelta(days=spreads.DTE_MAX)


class _SpreadData:
    """ReplayBroker seen as the spreads data source. The live chain carries the
    provider's delta; the synthetic quote has none, so the delta is the
    Black-Scholes delta of the IV implied by the synthetic mid."""

    def __init__(self, broker):
        self.b = broker

    def option_chain(self, underlying, expiry_min, expiry_max):
        from stockmoney.desk.options import select as sel
        now = self.b.clock.now
        spot = self.b.m.spot(underlying, now)
        out = []
        for c in self.b.option_chain(underlying, expiry_min, expiry_max):
            d = sel.contract_delta(c, spot, now)[0] if spot else None
            out.append(replace(c, delta=d))
        return out

    def option_quote(self, occ):
        return self.b.option_quote(occ)

    def stock_quote(self, symbol):
        return self.b.stock_quote(symbol)


def _spreads(start, end, cfg, cache_dir, feed, states):
    """Replay the H6 spread shadow day by day through ``spreads.tick`` (same
    entry, leg, fill and exit rules), quoting each leg from the options replay
    model: buys at the ask, sells at the bid, plus SLIP or shadow_slippage_pct.

    The book is rebuilt from ``_spread_warmup(start)`` so a spread opened before
    the window still blocks re-entry; only spreads entered inside the window
    count. A trading day with no cache is not ticked, and a spread that was open
    across it is kept out of the trades (its exit would be managed on a later
    session's quotes). Slots stop at the close and after the day's last bar."""
    from stockmoney.desk import store, wind
    from stockmoney.desk.options import replay as options_replay
    from stockmoney.desk.options import spreads
    trades, decisions, equity, missing = [], [], [25_000.0], []
    gapped: set[int] = set()
    mult = cfg.contract_multiplier
    first = date.fromisoformat(start)
    with TemporaryDirectory(prefix="stockmoney-spreads-") as work, store.connect(Path(work) / "spreads.sqlite") as con:
        spreads.ensure_schema(con)
        wind.ensure_schema(con)
        con.executemany("INSERT INTO wind_daily (date, state, recorded_at) VALUES (?,?, 'replay:daily_bars')",
                        sorted(states.items()))
        day = _spread_warmup(start)
        while day <= date.fromisoformat(end):
            if is_trading_day(day):
                if not (cache_dir / str(day)).is_dir():
                    if day >= first:
                        missing.append(str(day))
                    gapped.update(r[0] for r in con.execute(
                        "SELECT rowid FROM options_spreads_shadow WHERE status='OPEN'"))
                else:
                    market = options_replay.ReplayMarket(cache_dir / str(day), feed, cfg)
                    bars_end = _bars_end(market)
                    clock = options_replay.SimClock(next(_spread_slots(day)))
                    data = _SpreadData(options_replay.ReplayBroker(market, clock))
                    for slot in _spread_slots(day):
                        if bars_end is None or slot > bars_end + timedelta(minutes=1):
                            break  # no fresh underlying bar: the quotes would be frozen
                        clock.now = slot
                        spreads.tick(con, data, slot, cfg, slot)
                        if day < first:
                            continue
                        realized = con.execute("SELECT COALESCE(SUM(pnl_usd),0) FROM options_spreads_shadow "
                                               "WHERE status='CLOSED' AND day >= ?", (start,)).fetchone()[0]
                        held = sum((((r["last_value"] if r["last_value"] is not None else r["entry_debit"])
                                     - r["entry_debit"]) * mult - 2 * cfg.fee_per_contract) * r["qty"]
                                   - r["fees_usd"] for r in con.execute(
                                       "SELECT * FROM options_spreads_shadow WHERE status='OPEN' AND day >= ?",
                                       (start,)))
                        equity.append(25_000.0 + realized + held)
            day += timedelta(days=1)
        rows = con.execute("SELECT rowid AS rid, * FROM options_spreads_shadow WHERE day >= ? AND day <= ? "
                           "ORDER BY day, underlying", (start, end)).fetchall()
    excluded = still_open = gap = 0
    for r in rows:
        decisions.append({k: r[k] for k in ("day", "underlying", "source", "wind", "status", "skip_reason",
                                            "expiry", "long_strike", "short_strike", "entry_debit", "qty",
                                            "exit_reason")})
        if r["status"] == "OPEN":
            still_open += 1
        elif r["status"] == "CLOSED" and r["rid"] in gapped:
            gap += 1
        elif r["status"] == "CLOSED" and r["exit_reason"] in SPREAD_EXCLUDED:
            excluded += 1
        elif r["status"] == "CLOSED":
            trades.append({"symbol": r["underlying"], "strategy": f"spread:{r['source']}",
                           "entry_date": r["entry_time"], "exit_date": r["exit_time"], "pnl": r["pnl_usd"],
                           "r_multiple": r["pnl_usd"] / (r["entry_debit"] * mult * r["qty"]),
                           "fees": r["fees_usd"], "exit_reason": r["exit_reason"]})
    trades.sort(key=lambda t: (t["exit_date"], t["symbol"]))
    caveats = []
    if missing:
        caveats.append("missing_cache_days")
    if gap:
        caveats.append("spreads_open_across_missing_day_excluded")
    if excluded:
        caveats.append("expiry_settlements_excluded")
    if still_open:
        caveats.append("open_spreads_marked_in_drawdown_only")
    return trades, equity, decisions, caveats, missing


def run(sleeve: str, start: str, end: str, variant="incumbent", *, bars=None,
        cache_path: Path = Path("data/desk/replay.sqlite"),
        options_cache: Path = OptionsConfig().replay_cache, feed: str = "iex", stock_bps: float = 5.0,
        options_cfg: OptionsConfig | None = None) -> dict:
    if sleeve not in ("stocks", "options", "spreads") or date.fromisoformat(start) > date.fromisoformat(end):
        raise ValueError("invalid sleeve or date window")
    params, cfg, semantic, not_replayable = _variant(sleeve, variant)
    if options_cfg is not None:
        if variant != "incumbent":
            raise ValueError("cannot combine a proposal and a config override")
        cfg = options_cfg
    if sleeve == "stocks" and (not math.isfinite(stock_bps) or not 0 < stock_bps < 10_000):
        raise ValueError("stock cost must be positive bps per side")
    if sleeve != "stocks" and (not math.isfinite(cfg.fee_per_contract) or cfg.fee_per_contract <= 0
            or not math.isfinite(cfg.shadow_slippage_pct) or cfg.shadow_slippage_pct < 0):
        raise ValueError("options replay requires positive fees and nonnegative extra slippage")
    costs = ({"stock_bps_per_side": stock_bps} if sleeve == "stocks" else
             {"option_fee_per_contract": cfg.fee_per_contract,
              "option_extra_slippage_pct": cfg.shadow_slippage_pct, "option_spread": "synthetic_bid_ask"})
    # Config fingerprint prevents a test/custom config reusing an incumbent's cache.
    from dataclasses import asdict
    fingerprint = {"schema": 2, "sleeve": sleeve, "variant": semantic, "costs": costs}
    if sleeve != "stocks":
        ignored = {"halt_file", "lock_path", "report_dir", "replay_cache", "paper_only", "live_sources",
                   "capital_budget", "sleeve_pct", "max_open_positions", "max_live_per_underlying",
                   "entry_limit_offset", "entry_timeout_s", "exit_timeout_s", "exit_max_limit_attempts",
                   "cancel_max_failures", "cancel_page_every_min", "order_status_max_failures",
                   "flatten_budget_s", "flat_broker_max_attempts", "min_live_minutes_before_flat", "not_flat_alert_min"}
        fingerprint.update(options_config={k: v for k, v in asdict(cfg).items() if k not in ignored}, feed=feed)
    if sleeve == "spreads":
        from stockmoney.desk.options import spreads
        fingerprint.update(spread_rules={k: getattr(spreads, k) for k in (
            "SLIP", "DTE_MIN", "DTE_MAX", "DTE_TARGET", "LONG_DELTA", "LONG_BAND", "SHORT_DELTA", "SHORT_BAND",
            "TAKE_PROFIT_FRAC", "STOP_FRAC", "TIME_EXIT_DTE", "ENTRY_WINDOW", "WIND_SYMBOLS", "EXPIRY_SETTLE")},
            wind_source="daily_bars", spread_replay=2)
    digest = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, default=str).encode()).hexdigest()
    caveats = ["earnings_calendar_not_pit", "grok_plan_not_replayable", "grok_in_play_not_replayable"]
    if not_replayable:
        caveats.append("not_replayable")
        # A refusal is not evidence. Do not freeze it across later implementations.
        return _result(sleeve, start, end, digest, [], [], caveats, costs, status="not_replayable")
    cache_path = Path(cache_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(cache_path, timeout=1800) as cache:
        cache.execute(SCHEMA)
        # Serialize evaluation per cache, so concurrent requests cannot retest holdout.
        cache.execute("BEGIN IMMEDIATE")
        cached = cache.execute("SELECT result_json FROM replay_results WHERE variant_hash=? AND start=? AND end=?",
                               (digest, start, end)).fetchone()
        if cached:
            return json.loads(cached[0])
        if sleeve == "stocks":
            if bars is None:
                raise ValueError("stocks replay requires locally cached daily bars")
            replay = backtest.run(bars.upto(end), start, end, strategy_sets={"combined": list(REGISTRY)},
                                  params=params, slippage=stock_bps / 10_000)
            account = replay["accounts"]["combined"]
            caveats += ["current_universe_survivorship", "adjusted_daily_bars", "daily_decisions_after_close",
                        "trade_stats_closed_only_open_positions_marked_in_drawdown"]
            result = _result(sleeve, start, end, digest, account.trades,
                             [p["equity"] for p in account.equity_curve], caveats, costs, replay["decisions"])
        elif sleeve == "spreads":
            if bars is None:
                raise ValueError("spreads replay requires locally cached daily bars (the wind state)")
            trades, equity, decisions, extra, missing = _spreads(start, end, cfg, Path(options_cache), feed,
                                                        _wind_states(bars, start, end))
            caveats += extra + ["synthetic_quotes", "snapshot_iv_not_pit", "replay_delta_from_synthetic_mid",
                                "wind_from_adjusted_daily_bars", "signal_candidates_not_replayed",
                                "r_is_return_on_debit"]
            result = _result(sleeve, start, end, digest, trades, equity, caveats, costs, decisions,
                             missing_cache_dates=missing)
        else:
            trades, equity, decisions = _options(start, end, cfg, Path(options_cache), feed)
            caveats += ["synthetic_quotes", "snapshot_iv_not_pit", "contract_volume_filter_disabled",
                        "r_is_return_on_premium"]
            result = _result(sleeve, start, end, digest, trades, equity, caveats, costs, decisions)
        encoded = json.dumps(result, sort_keys=True, allow_nan=False)
        cache.execute("INSERT INTO replay_results VALUES (?,?,?,?)", (digest, start, end, encoded))
        return json.loads(encoded)
