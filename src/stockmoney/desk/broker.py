"""Paper broker: one simulated account, long-only stocks, daily bars.

Timing model (identical in backtest and live paper, so they can't drift):
- Signals are made from the close of day T.
- Entry/exit orders fill at the OPEN of day T+1, plus slippage.
- Protective stops are checked against each day's LOW; a gap below the stop
  fills at the open (the realistic, worse price), never at the stop.

This never talks to a real broker. It only books simulated fills.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import pandas as pd

SLIPPAGE = 0.0005  # 5 bps each side; large caps at the open
COMMISSION = 0.0


@dataclass
class Position:
    symbol: str
    strategy: str
    shares: int
    entry_date: str
    entry_price: float
    stop: float
    initial_stop: float
    highest_close: float
    signal_id: str = ""
    entry_index: int = 0  # bar index at entry, for holding-period counting

    def days_held(self, t: pd.Timestamp, index: pd.DatetimeIndex | None = None) -> int:
        if index is None:
            return int(pd.bdate_range(self.entry_date, t).size - 1)
        return int(index.get_loc(t) - self.entry_index)


@dataclass
class Order:
    order_id: str
    symbol: str
    side: str  # buy | sell
    strategy: str
    created: str
    shares: int = 0
    stop: float = 0.0
    reason: str = ""
    signal_id: str = ""
    status: str = "pending"  # pending_approval | pending | filled | cancelled | rejected | expired


@dataclass
class Account:
    name: str
    starting_cash: float
    cash: float
    risk_per_trade: float = 0.0075
    max_positions: int = 8
    max_position_pct: float = 0.20
    max_per_sector: int = 2
    positions: dict[str, Position] = field(default_factory=dict)
    orders: list[Order] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    equity_curve: list[dict] = field(default_factory=list)
    slippage: float = SLIPPAGE

    # ------------------------------------------------------------- valuation
    def equity(self, close: pd.Series) -> float:
        return self.cash + sum(p.shares * float(close.get(s, p.entry_price)) for s, p in self.positions.items())

    def invested(self, close: pd.Series) -> float:
        return sum(p.shares * float(close.get(s, p.entry_price)) for s, p in self.positions.items())

    # ------------------------------------------------------------- sizing
    def size(self, equity: float, entry: float, stop: float, exposure_cap: float,
             invested: float, target_weight: float | None, multiplier: float = 1.0) -> int:
        if entry <= 0 or exposure_cap <= 0:
            return 0
        if target_weight is not None:
            dollars = equity * target_weight * exposure_cap
        else:
            risk = entry - stop
            if risk <= 0:
                return 0
            dollars = equity * self.risk_per_trade / risk * entry
        dollars *= multiplier
        dollars = min(dollars, equity * self.max_position_pct, equity * exposure_cap - invested, self.cash)
        return max(0, math.floor(dollars / entry))

    # ------------------------------------------------------------- day cycle
    def process_open(self, t: pd.Timestamp, bar_open: pd.Series, bar_low: pd.Series, index: pd.DatetimeIndex | None = None) -> None:
        """Fill orders queued yesterday at today's open, then run stops."""
        day = str(t.date())
        for o in self.orders:
            if o.status != "pending":
                continue
            px = bar_open.get(o.symbol)
            if px is None or pd.isna(px):
                continue  # no bar today (halt / holiday for that symbol); keep waiting
            px = float(px)
            if o.side == "sell":
                if o.symbol in self.positions:
                    self._close(o.symbol, day, px * (1 - self.slippage), o.reason)
                o.status = "filled"
            else:
                if o.symbol in self.positions:
                    o.status = "cancelled"
                    o.reason += " | 已持有"
                    continue
                if px <= o.stop:
                    o.status = "cancelled"
                    o.reason += " | 開盤已跌破停損價,放棄進場"
                    continue
                fill = px * (1 + self.slippage)
                shares = min(o.shares, math.floor(self.cash / fill))
                if shares <= 0:
                    o.status = "cancelled"
                    o.reason += " | 現金不足"
                    continue
                self.cash -= shares * fill + COMMISSION
                self.positions[o.symbol] = Position(
                    o.symbol, o.strategy, shares, day, fill, o.stop, o.stop, fill,
                    o.signal_id, int(index.get_loc(t)) if index is not None else 0,
                )
                o.status = "filled"
        # anything not filled today and still pending_approval has expired
        for o in self.orders:
            if o.status == "pending_approval":
                o.status = "expired"
        self.orders = [o for o in self.orders if o.status in ("pending", "pending_approval")]

        for sym, p in list(self.positions.items()):
            low, op = bar_low.get(sym), bar_open.get(sym)
            if low is None or pd.isna(low) or low > p.stop:
                continue
            px = min(float(op), p.stop) if op is not None and not pd.isna(op) else p.stop
            self._close(sym, day, px * (1 - self.slippage), "觸發停損")

    def mark(self, t: pd.Timestamp, close: pd.Series) -> float:
        for sym, p in self.positions.items():
            c = close.get(sym)
            if c is not None and not pd.isna(c):
                p.highest_close = max(p.highest_close, float(c))
        eq = self.equity(close)
        self.equity_curve.append({"date": str(t.date()), "equity": eq, "cash": self.cash, "n_pos": len(self.positions)})
        return eq

    def _close(self, sym: str, day: str, px: float, reason: str) -> None:
        p = self.positions.pop(sym)
        self.cash += p.shares * px - COMMISSION
        pnl = (px - p.entry_price) * p.shares
        self.trades.append(
            {
                "symbol": sym, "strategy": p.strategy, "shares": p.shares,
                "entry_date": p.entry_date, "entry_price": p.entry_price,
                "exit_date": day, "exit_price": px, "pnl": pnl,
                "ret": px / p.entry_price - 1,
                "r_multiple": (px - p.entry_price) / (p.entry_price - p.initial_stop) if p.entry_price > p.initial_stop else float("nan"),
                "exit_reason": reason, "signal_id": p.signal_id,
            }
        )

    # ------------------------------------------------------------- persistence
    def to_dict(self) -> dict:
        d = asdict(self)
        d["positions"] = {k: asdict(v) for k, v in self.positions.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Account":
        d = dict(d)
        d["positions"] = {k: Position(**v) for k, v in d.get("positions", {}).items()}
        d["orders"] = [Order(**o) for o in d.get("orders", [])]
        return cls(**d)
