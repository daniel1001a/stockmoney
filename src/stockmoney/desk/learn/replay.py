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
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from stockmoney.desk import backtest
from stockmoney.desk.calendar import is_trading_day
from stockmoney.desk.options.config import GROK, OptionsConfig
from stockmoney.desk.strategies import REGISTRY

OPTION_PARAMS = {
    "gap_min_pct_index", "gap_min_pct_stock", "gap_go_min_rvol", "orb_break_volume_mult",
    "orb_min_day_rvol", "vwap_hold_bars", "vwap_slope_bars", "vwap_slope_min", "grok_min_rvol",
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
    if GROK in key or "in_play" in key:
        return {}, OptionsConfig(), semantic, True
    if sleeve == "stocks":
        pieces = key.split(".")
        if len(pieces) != 2 or pieces[0] not in REGISTRY or pieces[1] not in REGISTRY[pieces[0]].bounds:
            raise ValueError("unknown strategy parameter")
        name, param = pieces
        params = REGISTRY[name].validate_params({param: value})
        return {name: params}, OptionsConfig(), semantic, False
    if sleeve == "options" and key.startswith("options.") and key[8:] in OPTION_PARAMS:
        attr = key[8:]
        default = getattr(OptionsConfig(), attr)
        if value <= 0 or (isinstance(default, int) and value != int(value)):
            raise ValueError("option threshold must be positive, and whole for bar counts")
        return {}, replace(OptionsConfig(), **{attr: int(value) if isinstance(default, int) else value}), semantic, False
    raise ValueError("unknown or protected replay parameter")


def _drawdown(equity):
    peak, dd = 25_000.0, 0.0
    for value in equity:
        peak = max(peak, value)
        dd = max(dd, (peak - value) / peak)
    return dd


def _result(sleeve, start, end, digest, trades, equity, caveats, costs, decisions=(), status="ok"):
    rs = [t["r_multiple"] for t in trades]
    if any(not math.isfinite(r) for r in rs):
        raise ValueError("non-finite replay return")
    return {"tier": "replay", "sleeve": sleeve, "from": start, "to": end, "variant_hash": digest,
            "status": status, "trades": trades, "n": len(trades),
            "avg_r": sum(rs) / len(rs) if rs else None,
            "win_rate": sum(t["pnl"] > 0 for t in trades) / len(trades) if trades else None,
            "max_drawdown": _drawdown(equity), "caveats": sorted(set(caveats)), "costs": costs,
            "decisions": list(decisions)}


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
    if not math.isfinite(stock_bps) or not 0 < stock_bps < 10_000:
        raise ValueError("stock cost must be positive bps per side")
    if (not math.isfinite(cfg.fee_per_contract) or cfg.fee_per_contract <= 0
            or not math.isfinite(cfg.shadow_slippage_pct) or cfg.shadow_slippage_pct < 0):
        raise ValueError("options replay requires positive fees and nonnegative extra slippage")
    costs = {"stock_bps_per_side": stock_bps, "option_fee_per_contract": cfg.fee_per_contract,
             "option_extra_slippage_pct": cfg.shadow_slippage_pct, "option_spread": "synthetic_bid_ask"}
    # Config fingerprint prevents a test/custom config reusing an incumbent's cache.
    from dataclasses import asdict
    fingerprint = {"schema": 1, "sleeve": sleeve, "variant": semantic, "costs": costs,
                   "options_config": asdict(cfg), "feed": feed}
    digest = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, default=str).encode()).hexdigest()
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
        caveats = ["earnings_calendar_not_pit", "grok_plan_not_replayable", "grok_in_play_not_replayable"]
        if not_replayable or sleeve == "spreads":
            caveats.append("not_replayable")
            if sleeve == "spreads":
                caveats.append("spreads_replay_pending_issue_37")
            result = _result(sleeve, start, end, digest, [], [], caveats, costs, status="not_replayable")
        elif sleeve == "stocks":
            if bars is None:
                raise ValueError("stocks replay requires locally cached daily bars")
            replay = backtest.run(bars.upto(end), start, end, strategy_sets={"combined": list(REGISTRY)},
                                  params=params, slippage=stock_bps / 10_000)
            account = replay["accounts"]["combined"]
            caveats += ["current_universe_survivorship", "adjusted_daily_bars", "daily_decisions_after_close"]
            result = _result(sleeve, start, end, digest, account.trades,
                             [p["equity"] for p in account.equity_curve], caveats, costs, replay["decisions"])
        else:
            trades, equity, decisions = _options(start, end, cfg, Path(options_cache), feed)
            caveats += ["synthetic_quotes", "snapshot_iv_not_pit", "contract_volume_filter_disabled",
                        "r_is_return_on_premium"]
            result = _result(sleeve, start, end, digest, trades, equity, caveats, costs, decisions)
        encoded = json.dumps(result, sort_keys=True, allow_nan=False)
        cache.execute("INSERT INTO replay_results VALUES (?,?,?,?)", (digest, start, end, encoded))
        return json.loads(encoded)
