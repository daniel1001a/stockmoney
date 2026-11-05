"""vwap_reclaim debit-spread shadow variant (owner 2026-11-04). Shadow only:
nothing here sends, sizes or blocks an order, and no live path reads it.

At a vwap_reclaim entry the engine pairs the single-leg shadow position of the
live exit variant (position_id ``<setup>:shadow:<dte>:<live_exit>``) with a
vertical debit spread on the same expiry and right:
- long leg: the chosen contract, at the single leg's shadow entry price (ask
  plus shadow slippage);
- short leg: the strike further OTM whose |delta| is nearest SHORT_DELTA;
  when no further-OTM strike has a delta, the next strike further OTM. It is
  sold at the bid (minus shadow slippage) of one fresh two-sided quote.
qty = whole spreads within max_premium_per_trade at the debit.

Exits mirror the single leg: each _book_exit of the linked position closes the
same fraction of the spread (take-profit halves, stops, invalidation, time
stop, the forced flat and the session_end close). The long leg uses the
single leg's exit price; the short leg is bought back at its ask (plus
slippage) from one quote per exit event, falling back to the last known short
ask when that quote is stale (counted in stale_exits). Spread value is clamped
to [0, width]. Fees: fee_per_contract per contract per leg per side.

Storage: its own table, options_spread_variants (DDL in options/store.py).
The H6 table options_spreads_shadow is keyed UNIQUE(day, underlying) and its
rows are marked and exited by spreads.mark_and_exit under H6 rules, so a
vwap_reclaim spread there would collide with the H6 row and be managed by the
wrong exits.
"""
from __future__ import annotations

from datetime import datetime

from stockmoney.desk.options import select as sel
from stockmoney.desk.options import shadow as sh
from stockmoney.desk.options import store as ostore
from stockmoney.desk.options._iface import OptionContract
from stockmoney.desk.options.config import OptionsConfig

VARIANT = "vwap_reclaim_spread"
SOURCES = ("vwap_reclaim",)
SHORT_DELTA = 0.30
OPEN, EXITED = "OPEN", "EXITED"


def further_otm(chain: list[OptionContract], long: OptionContract) -> list[OptionContract]:
    """Same expiry and right, strikes further OTM, nearest first."""
    out = [c for c in chain if c.expiry == long.expiry and c.right == long.right and c.occ != long.occ
           and (c.strike > long.strike if long.right == "C" else c.strike < long.strike)]
    return sorted(out, key=lambda c: abs(c.strike - long.strike))


def pick_short(chain: list[OptionContract], long: OptionContract, spot: float,
               now: datetime) -> tuple[OptionContract, float | None, str] | None:
    cands = further_otm(chain, long)
    if not cands:
        return None
    best = None
    for c in cands:
        d, src = sel.contract_delta(c, spot, now)
        if d is not None and (best is None or abs(d - SHORT_DELTA) < abs(best[1] - SHORT_DELTA)):
            best = (c, d, src)
    return best if best is not None else (cands[0], None, "next_strike")


def get(con, position_id: str) -> dict | None:
    r = con.execute("SELECT * FROM options_spread_variants WHERE position_id=?", (position_id,)).fetchone()
    return dict(r) if r is not None else None


def open_spread(con, cfg: OptionsConfig, linked: dict, long: OptionContract, short: OptionContract,
                short_delta: float | None, delta_src: str, now: datetime) -> bool:
    """Insert the spread for ``linked`` (the single-leg shadow row). False when
    it already exists, the debit is not positive or one spread is over the cap."""
    long_px = linked["entry_price"]
    short_px = sh.sell_price(short.bid, cfg)
    debit = long_px - short_px
    width = abs(short.strike - long.strike)
    qty = sh.contracts_for(debit, cfg)
    if debit <= 0 or qty < 1 or get(con, linked["position_id"]) is not None:
        return False
    con.execute(
        "INSERT OR IGNORE INTO options_spread_variants (position_id, variant, day, source, mode, underlying, dte_label, "
        "expiry, right, long_occ, short_occ, long_strike, short_strike, width, short_delta, short_delta_source, qty, "
        "long_price, short_price, entry_debit, entry_time, open_frac, last_short_ask, status, realized_usd, fees_usd, "
        "pnl_usd, stale_exits) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (linked["position_id"], VARIANT, linked["day"], linked["source"], linked["mode"], linked["underlying"],
         linked["dte_label"], str(long.expiry), long.right, long.occ, short.occ, long.strike, short.strike, width,
         short_delta, delta_src, qty, long_px, short_px, debit, ostore.iso(now), 1.0, short.ask, OPEN, 0.0,
         2 * cfg.fee_per_contract * qty, None, 0))
    return True


def mirror_exit(con, cfg: OptionsConfig, row: dict, frac: float, long_exit_px: float,
                short_q: OptionContract | None, fresh: bool, reason: str, now: datetime) -> None:
    """Close ``frac`` (of the initial size) of the spread on the linked leg's exit."""
    frac = min(max(frac, 0.0), row["open_frac"])
    if frac <= 1e-9:
        return
    stale = not (fresh and short_q is not None and short_q.ask is not None)
    short_ask = row["last_short_ask"] if stale else short_q.ask
    short_px = sh.buy_price(short_ask, cfg) if short_ask is not None else 0.0
    value = min(max(long_exit_px - short_px, 0.0), row["width"])
    n = row["qty"] * frac
    m = cfg.contract_multiplier
    realized = (row["realized_usd"] or 0.0) + (value - row["entry_debit"]) * n * m
    fees = (row["fees_usd"] or 0.0) + 2 * cfg.fee_per_contract * n
    left = row["open_frac"] - frac
    fields = {"open_frac": left, "realized_usd": realized, "fees_usd": fees,
              "last_short_ask": short_ask, "stale_exits": (row["stale_exits"] or 0) + int(stale)}
    if left <= 1e-9:
        fields.update(open_frac=0.0, status=EXITED, exit_time=ostore.iso(now), exit_reason=reason,
                      pnl_usd=realized - fees)
    sets = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE options_spread_variants SET {sets} WHERE position_id=?", (*fields.values(), row["position_id"]))


def open_rows(con, day: str) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM options_spread_variants WHERE day=? AND status=?",
                                         (day, OPEN))]


def summary(con) -> list[dict]:
    """Per dte_label: n exited, total P&L after fees (report only)."""
    rows = con.execute("SELECT dte_label, COUNT(*), SUM(pnl_usd), COUNT(DISTINCT day) FROM options_spread_variants "
                       "WHERE status=? GROUP BY dte_label ORDER BY dte_label", (EXITED,)).fetchall()
    return [{"dte_label": r[0], "n": r[1], "pnl": r[2] or 0.0, "days": r[3]} for r in rows]
