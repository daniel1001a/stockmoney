"""Shadow exit model: what a different exit-limit rule would have done on the
same live (paper) exits, priced from the quotes the sleeve recorded.

Read only. It never sends, replaces or cancels an order and changes nothing
the engine does; live exits keep their current rule (engine._send_exit: a sell
limit at the bid rounded down to the tick, repriced after exit_timeout_s,
market after exit_max_limit_attempts unfilled limits or at force-flat).

An exit episode is the run of exit orders on one position that ends at a fill.
Its decision bid is the first order's recorded ref_bid (older rows without it
fall back to that order's limit price, which was the bid rounded down to the
tick). The chase cost is (decision bid - fill) x qty x multiplier: positive
means the fill came in below the bid the sleeve decided on.

Alternative rule ("urgent cross"): for stop-type exits (every reason except
take_profit) the first limit goes max(one tick, half the quoted spread) below
the bid, so a stale indicative bid does not leave the order working while the
market moves away. Take-profit keeps the bid. Simulation, conservative:
  * the first live order filled: a lower sell limit would have filled too, at
    no worse than the market the live fill shows, so the alternative books the
    same price ("same");
  * the first live order was repriced: if the next recorded bid (the
    replacement's ref_bid or limit) is at or above the alternative limit, the
    market was there by the reprice at the latest, so the alternative books its
    own limit ("filled"); otherwise it cannot be told from the polled quotes
    and the episode keeps the live price ("undetermined").
Quotes are Alpaca indicative polls, so every number here is a reference only.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from stockmoney.desk.options import shadow as sh
from stockmoney.desk.options import store as ostore
from stockmoney.desk.options.config import OptionsConfig

PATIENT = ("take_profit",)  # exits that keep the bid under the alternative rule


def _bid(o: dict) -> float | None:
    return o.get("ref_bid") or o.get("limit_price")


def alt_limit(underlying: str, bid: float, ask: float | None, reason: str) -> float:
    if reason in PATIENT:
        return bid
    tick = sh.option_tick(underlying, bid)
    off = max(tick, (ask - bid) / 2 if ask and ask > bid else 0.0)
    return max(sh.round_tick(bid - off, tick, up=False), tick)


def episodes(con, position: dict, cfg: OptionsConfig | None = None) -> list[dict]:
    cfg = cfg or OptionsConfig()
    mult = cfg.contract_multiplier
    out, cur = [], []
    for o in ostore.orders_for(con, position["position_id"]):
        if o["kind"] != "exit" or o["side"] != "sell":
            continue
        cur.append(o)
        if not (o["filled_qty"] or 0) > 0:
            continue
        first, last = cur[0], o
        bid0 = _bid(first)
        if bid0 and last["avg_price"]:
            qty = last["filled_qty"]
            fill = last["avg_price"]
            alt = alt_limit(position["underlying"], bid0, first.get("ref_ask"), last["reason"])
            if len(cur) == 1 or alt == first["limit_price"]:  # filled at once, or the rule is the live one
                status, alt_fill = "same", fill
            else:
                nxt = _bid(cur[1])
                status, alt_fill = (("filled", alt) if nxt is not None and nxt >= alt else ("undetermined", fill))
            secs = (datetime.fromisoformat(last["submitted_at"])
                    - datetime.fromisoformat(first["submitted_at"])).total_seconds()
            out.append({
                "position_id": position["position_id"], "setup": position["source"], "underlying": position["underlying"],
                "reason": last["reason"], "qty": qty, "bid0": bid0, "bid_source": "ref_bid" if first.get("ref_bid") else "limit",
                "fill": fill, "orders": len(cur), "secs": secs,
                "chase_usd": round((bid0 - fill) * qty * mult, 2),
                "alt_limit": alt, "alt_status": status, "alt_fill": alt_fill,
                "alt_minus_live_usd": round((alt_fill - fill) * qty * mult, 2)})
        cur = []
    return out


def compare(con, positions: list[dict], cfg: OptionsConfig | None = None) -> list[dict]:
    """Exit episodes of the given live positions (shadow positions are skipped)."""
    return [e for p in positions if p["mode"] == "live" for e in episodes(con, p, cfg)]


def by_setup(rows: list[dict]) -> list[dict]:
    agg: dict[str, dict] = defaultdict(lambda: {"n": 0, "repriced": 0, "chase_usd": 0.0, "alt_minus_live_usd": 0.0,
                                                "undetermined": 0})
    for r in rows:
        a = agg[r["setup"]]
        a["n"] += 1
        a["repriced"] += r["orders"] > 1
        a["chase_usd"] += r["chase_usd"]
        a["alt_minus_live_usd"] += r["alt_minus_live_usd"]
        a["undetermined"] += r["alt_status"] == "undetermined"
    return [{"setup": k, **v} for k, v in sorted(agg.items())]


def report_lines(rows: list[dict], caveat: str) -> list[str]:
    if not rows:
        return []
    lines = ["## 出場追價(影子出場規則對照,實盤不變)", "", f"> {caveat}",
             "> 對照規則:停損類出場首張限價掛在買價下 max(1 tick, 半個價差);停利維持買價。"
             "首張就成交記為相同;被改價的,下一個記錄到的買價不低於對照限價才記成交,否則記為無法判定。", "",
             "| 劇本 | 出場筆數 | 改價筆數 | 追價成本 $ | 對照規則 - 實際 $ | 無法判定 |", "|---|---|---|---|---|---|"]
    for a in by_setup(rows):
        lines.append(f"| {a['setup']} | {a['n']} | {a['repriced']} | {a['chase_usd']:+.0f} | "
                     f"{a['alt_minus_live_usd']:+.0f} | {a['undetermined']} |")
    return lines + [""]
