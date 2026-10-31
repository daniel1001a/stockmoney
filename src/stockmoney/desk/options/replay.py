"""Historical replay: run one full day through the SAME Engine as the live
session, with a simulated clock and a simulated broker built from cached
1-minute bars (marketdata.fetch_replay_cache).

What is real and what is modelled (stated in every replay report):
- underlying 1-minute bars: REAL (Alpaca historical, IEX by default to match
  the live session's feed; SIP optional), including the previous sessions
  the RVOL profile needs;
- option prices: SYNTHETIC QUOTE MODEL. There is no historical option
  bid/ask at all. Option 1-minute TRADE bars can be read for a past session
  (verified 2026-10-12 after the close), but not in real time (a window
  touching the last 30 minutes answers HTTP 403 "OPRA agreement is not
  signed"), so the live session never uses them. In a replay the mid is
  the contract's last trade bar close when it traded within
  ``BAR_MAX_AGE`` (source "trade_bar"); otherwise Black-Scholes at the
  current underlying bar close with an IV taken, in order, from: the IV
  implied by the contract's last trade bar, its IV in the indicative
  snapshot taken when the cache was fetched (AFTER the day: a mild
  look-ahead in the IV level only), the nearest strike's IV of the same
  expiry and right, else ``cfg.default_iv``. Bid/ask is ALWAYS mid -/+ a
  modelled half spread (``half_spread``), never a real quote. Volume is 0
  (unknown), so the contract volume filter is off in a replay. The source
  of every quote is counted and printed in the report header.
- the underlying quote is the last bar close -/+ 1 bp (synthetic);
- order fills: a buy limit fills at the modelled ask once the limit reaches
  it, a sell limit at the modelled bid; market orders fill immediately;
- the risk gate is line A's real OptionsRiskEngine (via _iface.LineAGate)
  on the replay database, with the contract volume filter off because the
  synthetic quotes carry no traded volume.
Shadow results from a replay therefore measure the engine's mechanics and the
trigger logic on real price paths, NOT a trustworthy expectancy: the spread
and the option price path are modelled.
"""
from __future__ import annotations

import itertools
import json
import math
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from stockmoney.desk import store
from stockmoney.desk.options import plan as oplan
from stockmoney.desk.options import select as sel
from stockmoney.desk.options import store as ostore
from stockmoney.desk.options import _iface
from stockmoney.desk.options._iface import (
    AccountSnapshot, AssetClass, BrokerOrder, BrokerPosition, OptionContract, OrderIntent, OrderStatus, OrderType,
    Side,
)
from stockmoney.desk.options.config import OptionsConfig
from stockmoney.desk.options.engine import Engine
from stockmoney.desk.options.marketdata import bars_frame

ET = ZoneInfo("America/New_York")
BAR_MAX_AGE = timedelta(minutes=10)
ONE_MIN = pd.Timedelta(minutes=1)


class SimClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def half_spread(mid: float, underlying: str, cfg: OptionsConfig) -> float:
    """Modelled half spread: index ETFs 1.5% of the mid (at least 1 cent),
    single stocks 3% (at least 2.5 cents). Chosen from the 2026-10-12 SPY
    snapshot (0.63 x 0.64) and typical single-stock weeklies; NOT measured."""
    if underlying in cfg.index:
        return max(0.01, 0.015 * mid)
    return max(0.025, 0.03 * mid)


class ReplayMarket:
    def __init__(self, cache_dir: Path, feed: str, cfg: OptionsConfig):
        self.cfg = cfg
        st = pd.read_parquet(cache_dir / f"stock_{feed}.parquet")
        self.stock = {s: bars_frame(g.rename(columns={"open": "o", "high": "h", "low": "l", "close": "c",
                                                       "volume": "v", "vwap": "vw"}).to_dict("records"))
                      for s, g in st.groupby("symbol")}
        c = pd.read_parquet(cache_dir / "contracts.parquet")
        self.contracts = {r.occ: r for r in c.itertuples()}
        obp = cache_dir / "option_bars.parquet"
        ob = pd.read_parquet(obp) if obp.exists() else pd.DataFrame()
        self.obars = {k: bars_frame(g.to_dict("records")) for k, g in ob.groupby("occ")} if "occ" in ob else {}
        self.meta = json.loads((cache_dir / "meta.json").read_text())
        self._iv_cache: dict[str, tuple[float, str]] = {}
        sip = cache_dir / "stock_sip.parquet"
        self.sip = {}
        if sip.exists():
            st2 = pd.read_parquet(sip)
            self.sip = {s: bars_frame(g.rename(columns={"open": "o", "high": "h", "low": "l", "close": "c",
                                                         "volume": "v", "vwap": "vw"}).to_dict("records"))
                        for s, g in st2.groupby("symbol")}
        self.feed = feed

    def spot(self, sym: str, now: datetime) -> float | None:
        b = self.stock.get(sym)
        if b is None:
            return None
        i = b.index.searchsorted(pd.Timestamp(now) - ONE_MIN, side="right")  # bars closed by now
        return float(b["close"].iloc[i - 1]) if i else None

    def _last_bar(self, occ: str, now: datetime):
        b = self.obars.get(occ)
        if b is None:
            return None
        i = b.index.searchsorted(pd.Timestamp(now) - ONE_MIN, side="right")
        return None if not i else (b.index[i - 1], b.iloc[i - 1], float(b["volume"].iloc[:i].sum()))

    def _iv_from_bar(self, occ: str, now: datetime) -> float | None:
        lb = self._last_bar(occ, now)
        r = self.contracts[occ]
        if lb is None:
            return None
        t_bar = (lb[0] + ONE_MIN).to_pydatetime()
        s = self.spot(r.underlying, t_bar)
        if not s:
            return None
        return sel.implied_vol(float(lb[1]["close"]), s, r.strike, sel.years_to_expiry(date.fromisoformat(r.expiry),
                                                                                       t_bar), r.right)

    def _snap_iv(self, occ: str) -> float | None:
        r = self.contracts[occ]
        v = getattr(r, "iv_snapshot", None)
        return float(v) if v is not None and not pd.isna(v) and v > 0 else None

    def _model_iv(self, occ: str, now: datetime) -> tuple[float, str]:
        if not self.obars:  # no trade bars at all: the answer does not depend on the time
            if occ not in self._iv_cache:
                self._iv_cache[occ] = self._model_iv_at(occ, now)
            return self._iv_cache[occ]
        return self._model_iv_at(occ, now)

    def _model_iv_at(self, occ: str, now: datetime) -> tuple[float, str]:
        r = self.contracts[occ]
        iv = self._iv_from_bar(occ, now)
        if iv:
            return iv, "bs_trade_iv"
        iv = self._snap_iv(occ)
        if iv:
            return iv, "bs_snapshot_iv"
        peers = sorted((c for c in self.contracts.values() if c.underlying == r.underlying and c.expiry == r.expiry
                        and c.right == r.right and c.occ != occ), key=lambda c: abs(c.strike - r.strike))
        for c in peers[:6]:
            iv = self._snap_iv(c.occ) or self._iv_from_bar(c.occ, now)
            if iv:
                return iv, "bs_peer_iv"
        return self.cfg.default_iv, "bs_default_iv"

    def quote(self, occ: str, now: datetime) -> tuple[OptionContract | None, str]:
        """(synthetic quote, source label). See the module docstring."""
        r = self.contracts.get(occ)
        if r is None:
            return None, "unknown"
        spot = self.spot(r.underlying, now)
        if spot is None:
            return None, "no_spot"
        expiry = date.fromisoformat(r.expiry)
        if datetime.combine(expiry, time(16, 0), ET) <= now:
            return None, "expired"
        lb = self._last_bar(occ, now)
        if lb is not None and (pd.Timestamp(now) - (lb[0] + ONE_MIN)) <= BAR_MAX_AGE:
            mid, src = float(lb[1]["close"]), "trade_bar"
        else:
            iv, src = self._model_iv(occ, now)
            mid = sel.bs_price(spot, r.strike, sel.years_to_expiry(expiry, now), iv, r.right)
        if not math.isfinite(mid) or mid <= 0:
            return None, src
        h = half_spread(mid, r.underlying, self.cfg)
        bid = max(round(mid - h, 2), 0.0)
        ask = max(round(mid + h, 2), bid + 0.01)
        oi = None if pd.isna(r.open_interest) else int(r.open_interest)
        return OptionContract(occ, r.underlying, r.right, float(r.strike), expiry, bid, ask, mid, 0, oi, None,
                              None, now - timedelta(seconds=1)), src


class ReplayBroker:
    """Simulated paper broker over a ReplayMarket. Implements the
    OptionsTradingBroker surface; never talks to any real broker."""

    name = "replay"
    is_paper = True

    def __init__(self, market: ReplayMarket, clock: SimClock):
        self.m = market
        self.clock = clock
        self.orders: dict[str, BrokerOrder] = {}
        self.positions: dict[str, float] = {}
        self.sources: dict[str, int] = {}
        self._ids = itertools.count(1)

    # ---- data
    def minute_bars(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        b = self.m.stock.get(symbol)
        if b is None:
            return bars_frame([])
        lim = min(pd.Timestamp(end), pd.Timestamp(self.clock.now))
        return b[(b.index >= pd.Timestamp(start)) & (b.index + ONE_MIN <= lim)].copy()

    def premarket_dollar_volume(self, symbol: str, day: date, now: datetime) -> float | None:
        """Same contract as the live adapter: SIP premarket, 16 minutes delayed."""
        b = self.m.sip.get(symbol)
        if b is None:
            return None
        end = min(pd.Timestamp(datetime.combine(day, time(9, 30), ET)), pd.Timestamp(now) - pd.Timedelta(minutes=16))
        b = b[(b.index >= pd.Timestamp(datetime.combine(day, time(4, 0), ET))) & (b.index + ONE_MIN <= end)]
        return float((b["close"] * b["volume"]).sum())

    def stock_quote(self, symbol: str) -> tuple[float, float] | None:
        s = self.m.spot(symbol, self.clock.now)
        return (s * 0.9999, s * 1.0001) if s else None

    def _q(self, occ: str) -> OptionContract | None:
        q, src = self.m.quote(occ, self.clock.now)
        self.sources[src] = self.sources.get(src, 0) + 1
        return q

    def option_chain(self, underlying: str, expiry_min: date, expiry_max: date) -> list[OptionContract]:
        out = []
        for occ, r in self.m.contracts.items():
            if r.underlying == underlying and expiry_min <= date.fromisoformat(r.expiry) <= expiry_max:
                q = self._q(occ)
                if q is not None:
                    out.append(q)
        return out

    def option_quote(self, occ: str) -> OptionContract | None:
        return self._q(occ)

    # ---- orders
    def submit(self, intent: OrderIntent) -> BrokerOrder:
        if intent.client_order_id in self.orders:
            return replace(self.orders[intent.client_order_id])
        if intent.side == Side.SELL and intent.qty > self.positions.get(intent.symbol, 0) + 1e-9:
            raise RuntimeError("insufficient qty (replay broker: no short options)")
        o = BrokerOrder(str(next(self._ids)), intent.client_order_id, intent.symbol, intent.side, OrderStatus.NEW,
                        intent.qty, 0.0, None, self.clock.now, None, intent.order_type, intent.tif,
                        None, intent.limit_price, AssetClass.OPTION)
        self.orders[o.client_order_id] = o
        self._try_fill(o)
        return replace(o)

    def _try_fill(self, o: BrokerOrder) -> None:
        if o.status not in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED):
            return
        q = self._q(o.symbol)
        if q is None:
            return
        px = q.ask if o.side == Side.BUY else q.bid
        if o.order_type == OrderType.LIMIT and ((o.side == Side.BUY and px > o.limit_price + 1e-9)
                                                or (o.side == Side.SELL and px < o.limit_price - 1e-9)):
            return
        o.filled_qty, o.filled_avg_price, o.filled_at, o.status = o.qty, px, self.clock.now, OrderStatus.FILLED
        sign = 1 if o.side == Side.BUY else -1
        self.positions[o.symbol] = self.positions.get(o.symbol, 0) + sign * o.qty
        if abs(self.positions[o.symbol]) < 1e-9:
            del self.positions[o.symbol]

    def get_order(self, client_order_id: str) -> BrokerOrder | None:
        o = self.orders.get(client_order_id)
        if o is None:
            return None
        self._try_fill(o)
        return replace(o)

    def cancel(self, client_order_id: str) -> None:
        o = self.orders.get(client_order_id)
        if o is not None and o.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED):
            o.status = OrderStatus.CANCELED

    def open_orders(self) -> list[BrokerOrder]:
        return [replace(o) for o in self.orders.values()
                if o.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED, OrderStatus.PENDING)]

    def account(self) -> AccountSnapshot:
        pos = []
        for s, q in self.positions.items():
            k = self._q(s)
            m = k.mid if k is not None and k.mid is not None else 0.0
            pos.append(BrokerPosition(s, AssetClass.OPTION, q, 0.0, q * m * 100, m))
        return AccountSnapshot(0.0, 0.0, 0.0, pos, self.clock.now)


@dataclass
class ReplayResult:
    db_path: Path
    report: str
    quote_sources: dict
    notifications: list
    shadow_equity: list[float] = field(default_factory=list)


def run(day: date, cfg: OptionsConfig, cache_dir: Path, out_dir: Path, feed: str = "iex", step_s: float = 20.0,
        plan_file: Path | None = None, plan_received_at: datetime | None = None,
        entries_blocked: bool = False) -> ReplayResult:
    """``entries_blocked``: the desk's research settings did not validate, so
    the live engine would take no new entries; the replay must not either."""
    from stockmoney.desk.options import report as oreport
    out_dir.mkdir(parents=True, exist_ok=True)
    db = out_dir / f"replay_{day}_{feed}.sqlite"
    db.unlink(missing_ok=True)
    # synthetic quotes carry no traded volume: the contract volume filter is off (stated in the header)
    cfg = replace(cfg, halt_file=out_dir / "NO_HALT_IN_REPLAY", min_volume=0)
    market = ReplayMarket(cache_dir / str(day), feed, cfg)
    clock = SimClock(datetime.combine(day, time(*cfg.session_start), ET).astimezone(timezone.utc))
    broker = ReplayBroker(market, clock)
    sent: list = []
    equity = [25_000.0]
    with store.connect(db) as con:
        ostore.ensure_schema(con)
        if plan_file is not None:
            rec = plan_received_at or datetime.combine(day, time(8, 0), ET)
            res = oplan.ingest(con, plan_file.read_text(encoding="utf-8"), rec, cfg)
            ostore.event(con, str(day), clock.now, "info", "plan", f"replay plan ingest: {res}")
        from stockmoney.desk.learn.registry import safe_options_config
        cfg = safe_options_config(con, cfg, paper=True, on_error=lambda *a: sent.append(a))
        gate = _iface.make_risk_gate(con, broker, cfg)
        eng = Engine(con, broker, cfg, clock=clock, risk_gate=gate, live=True, notifier=lambda *a: sent.append(a),
                     config_resolved=True, entries_blocked=entries_blocked)
        end = eng.session_end(clock.now)  # the close (early-close aware)
        while clock.now <= end:
            eng.tick()
            # Mark one DTE/exit arm per rule using the same liquidation costs
            # as shadow fills. Do not pool the experimental duplicate orb arm.
            rows = con.execute("SELECT realized_usd,fees_usd,qty_open,last_bid,entry_price FROM options_positions "
                               "WHERE mode='shadow' AND dte_label=? AND exit_variant=? "
                               "AND source NOT IN ('grok_plan','orb15_5m')",
                               (cfg.live_dte, cfg.live_exit)).fetchall()
            pnl = sum((r[0] or 0) - (r[1] or 0) + (r[2] or 0) * (
                ((r[3] or 0) * (1 - cfg.shadow_slippage_pct) - (r[4] or 0)) * cfg.contract_multiplier
                - cfg.fee_per_contract) for r in rows)
            equity.append(25_000.0 + pnl)
            clock.advance(step_s)
        eng.finish()
        header = (f"> 重播模式:{day},標的 K 線為真實資料(Alpaca {feed.upper()})。期權報價是**合成模型**:沒有歷史買賣價;"
                  f"中價優先用該合約 10 分鐘內最近一根歷史 1 分鐘成交價(trade_bar,盤後才查得到,盤中即時查不到),"
                  f"否則用 Black-Scholes(標的當下價格,IV 依序取自該合約成交價反推、抓取快取時的 indicative 快照、鄰近履約價、"
                  f"預設值;快照 IV 屬盤後資料,IV 水準有輕微 look-ahead)。買賣價差一律是模型假設,成交量未知(重播中關閉成交量門檻)。"
                  f"影子損益只能檢查引擎機制與觸發邏輯,不能當作期望值證據。"
                  f"風控為 A 線正式風控(OptionsRiskEngine),但合成報價沒有成交量,成交量門檻在重播中關閉。報價來源次數:{json.dumps(broker.sources)}")
        rep = oreport.build(con, str(day), cfg, header=header)
    return ReplayResult(db, rep, dict(broker.sources), sent, equity)


# ------------------------------------------------------------------ synthetic day (selftest and tests)

def synthetic_cache(out_dir: Path, day: date, symbol: str = "SPY") -> Path:
    """A fully synthetic, clearly fake day for offline checks: ``symbol``
    opens at 600.5, holds a 600 to 601 range for 15 minutes, then rallies to
    606 by 11:00 on rising volume (a 3x spike on the first close above the
    range) and drifts. The previous 21 sessions trade 50,000 shares a minute
    from 09:30 to 11:30 (the RVOL history). Option contracts for the 0DTE
    and next-day expiries, strikes 590 to 615, with NO option bars (so every
    quote is the Black-Scholes model at ``default_iv``)."""
    from stockmoney.desk.options._iface import occ_symbol
    from stockmoney.desk.options.engine import _prev_trading_day
    from stockmoney.desk.options.select import trading_day_offset
    d = out_dir / str(day)
    d.mkdir(parents=True, exist_ok=True)
    rows = []
    prev = h = _prev_trading_day(day)
    for _ in range(21):
        for m in range(120):
            rows.append((datetime.combine(h, time(9, 30), ET) + timedelta(minutes=m), 600.0, 600.5, 599.5, 600.0,
                         50_000))
        h = _prev_trading_day(h)
    for m in range(10):
        t = datetime.combine(prev, time(15, 50), ET) + timedelta(minutes=m)
        rows.append((t, 600.0, 600.1, 599.9, 600.0, 50_000))
    for m in range(90):  # 08:00 to 09:29 premarket, about $20M
        t = datetime.combine(day, time(8, 0), ET) + timedelta(minutes=m)
        rows.append((t, 600.4, 600.5, 600.3, 600.4, 400))
    px, spiked = 600.5, False
    for m in range(390):
        t = datetime.combine(day, time(9, 30), ET) + timedelta(minutes=m)
        if m < 15:
            o, c, v = px, 600.5 + (0.4 if m % 2 else -0.4), 100_000
        elif m < 90:
            o, c, v = px, px + 0.075, 150_000 + 2_000 * (m - 15)
            if c > 600.95 and not spiked:
                v, spiked = 3 * v, True
        else:
            o, c, v = px, px + (0.01 if m % 2 else -0.01), 60_000
        rows.append((t, o, max(o, c) + 0.05, min(o, c) - 0.05, c, v))
        px = c
    df = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "volume"])
    df["vwap"] = (df.high + df.low + df.close) / 3
    df["symbol"] = symbol
    df["t"] = pd.to_datetime(df["t"], utc=True)
    for feed in ("sip", "iex"):
        df.to_parquet(d / f"stock_{feed}.parquet", index=False)
    crow = []
    for exp in (day, trading_day_offset(day, 1)):
        for k in range(590, 616):
            for right in ("C", "P"):
                crow.append({"occ": occ_symbol(symbol, exp, right, k), "underlying": symbol, "right": right,
                             "strike": float(k), "expiry": str(exp), "open_interest": 5000,
                             "open_interest_date": str(prev)})
    pd.DataFrame(crow).to_parquet(d / "contracts.parquet", index=False)
    pd.DataFrame(columns=["occ", "t", "o", "h", "l", "c", "v", "vw"]).to_parquet(d / "option_bars.parquet", index=False)
    (d / "meta.json").write_text(json.dumps({"day": str(day), "synthetic": True, "symbols": [symbol]}))
    return d


def example_plan(day: date, symbol: str = "SPY") -> str:
    """A valid OPTIONS_PLAN for the synthetic day (break above 602)."""
    gen = datetime.combine(day, time(7, 45), ET).isoformat()
    return json.dumps({
        "protocol": "1", "type": "OPTIONS_PLAN", "plan_id": f"gp-{day:%Y%m%d}-test", "for_date": str(day),
        "generated_at": gen, "market_context": {"summary": "synthetic", "risk_tone": "mixed", "key_events": []},
        "plans": [{"underlying": symbol, "bias": "long", "playbook": "orb", "thesis": "synthetic breakout",
                   "catalyst": "", "trigger": {"kind": "break_above", "level": 602.0, "confirm_closes": 2,
                                               "earliest": "09:45", "latest": "11:00"},
                   "invalidation": {"kind": "below", "underlying_level": 599.5}, "target_underlying_pct": 0.8,
                   "max_hold_minutes": 120, "preferred_dte": "0dte", "confidence": 0.5, "evidence_urls": []}],
        "avoid": [], "notes": ""}, separators=(",", ":"))
