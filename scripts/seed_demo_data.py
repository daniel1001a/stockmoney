"""Deterministic demo-data seeder for the redesigned frontend.

Fresh checkouts have no DuckDB and no live feed, so there is nothing to render
or verify a UI against. This script builds a *realistic, self-consistent*
snapshot -- price history, daily model calls (with graded win/loss history),
backtest snapshots, a multi-type news feed, an event calendar, an expanded
trader roster with graded league predictions, and per-trader virtual options
portfolios (trades + open positions) -- so the whole product is demonstrable
end-to-end.

It is a DEMO/dev fixture, not production data. Every row is synthetic and
labelled as such via source='demo_seed'. It respects the same hard boundary as
the rest of the system: it only writes to the analysis/discretion tables, never
places an order, and the "trades" in trader_portfolios are simulated contest
fills used purely for scoring.

Usage:
    .venv/bin/python scripts/seed_demo_data.py            # -> data/stockmoney.duckdb
    .venv/bin/python scripts/seed_demo_data.py --db :memory:   # smoke test
"""
from __future__ import annotations

import argparse
import json
import math
import random
import uuid
from datetime import date, datetime, timedelta, timezone

from stockmoney.data.db import DEFAULT_DB_PATH, get_connection, run_migrations

RNG = random.Random(20260712)
NOW = datetime(2026, 7, 12, 13, 0, tzinfo=timezone.utc)
TODAY = date(2026, 7, 10)  # last trading day (Fri) before the 2026-07-12 "today"
SOURCE = "demo_seed"

# symbol -> (sector, base price, annualised drift, daily vol)
UNIVERSE = {
    "NVDA": ("semiconductor", 178.0, 0.35, 0.032),
    "AVGO": ("semiconductor", 285.0, 0.28, 0.028),
    "AMD": ("semiconductor", 172.0, 0.10, 0.038),
    "TSM": ("semiconductor", 205.0, 0.22, 0.026),
    "SOXL": ("semiconductor_etf", 42.0, 0.30, 0.060),
    "SOXS": ("semiconductor_etf", 8.2, -0.35, 0.060),
    "AAPL": ("big_tech", 232.0, 0.12, 0.018),
    "MSFT": ("big_tech", 468.0, 0.18, 0.019),
    "GOOGL": ("big_tech", 195.0, 0.20, 0.022),
    "META": ("big_tech", 690.0, 0.24, 0.024),
    "AMZN": ("big_tech", 224.0, 0.16, 0.023),
}

REGIME_LABELS = {0: "震盪盤整", 1: "趨勢多頭", 2: "趨勢空頭"}
DIRECTIONS = ("down", "range", "up")


def business_days(end: date, n: int) -> list[date]:
    days: list[date] = []
    d = end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def gen_prices(symbol: str) -> list[tuple[date, float, float, float, float, int]]:
    """90 business days of OHLCV as a mild geometric random walk."""
    sector, base, drift, vol = UNIVERSE[symbol]
    days = business_days(TODAY, 90)
    rng = random.Random(hash((symbol, "px")) & 0xFFFFFFFF)
    price = base * (1 - 0.12)  # start ~12% below "today" so the trend lands near base
    rows = []
    mu = drift / 252
    for d in days:
        shock = rng.gauss(mu, vol)
        prev = price
        price = max(0.5, price * (1 + shock))
        hi = max(prev, price) * (1 + abs(rng.gauss(0, vol / 3)))
        lo = min(prev, price) * (1 - abs(rng.gauss(0, vol / 3)))
        volume = int(rng.uniform(2e7, 9e7))
        rows.append((d, round(prev, 2), round(hi, 2), round(lo, 2), round(price, 2), volume))
    return rows


def fetch_real_prices() -> dict[str, list[tuple[date, float, float, float, float, int]]]:
    """Real ~90-trading-day OHLCV from yfinance for the whole watchlist. Returns
    {} on any failure so the caller falls back to the synthetic walk -- the seed
    must still work fully offline."""
    import warnings

    warnings.filterwarnings("ignore")
    try:
        import yfinance as yf
    except ImportError:
        return {}
    out: dict[str, list] = {}
    try:
        raw = yf.download(
            list(UNIVERSE), period="140d", interval="1d",
            group_by="ticker", auto_adjust=False, progress=False, threads=True,
        )
    except Exception:
        return {}
    for symbol in UNIVERSE:
        try:
            sub = raw[symbol].dropna(subset=["Close"])
        except Exception:
            continue
        rows = []
        for ts, r in sub.iterrows():
            d = ts.date()
            if d > TODAY:
                continue
            rows.append((
                d, round(float(r["Open"]), 2), round(float(r["High"]), 2),
                round(float(r["Low"]), 2), round(float(r["Close"]), 2),
                int(r["Volume"]) if r["Volume"] == r["Volume"] else 0,
            ))
        if len(rows) >= 30:  # need enough history for charts/features
            out[symbol] = rows[-90:]
    return out


def seed_prices(conn, *, use_real: bool = True) -> dict[str, list[tuple[date, float]]]:
    real = fetch_real_prices() if use_real else {}
    if real:
        print(f"  using REAL yfinance prices for {len(real)}/{len(UNIVERSE)} symbols")
    closes: dict[str, list[tuple[date, float]]] = {}
    for symbol in UNIVERSE:
        rows = real.get(symbol) or gen_prices(symbol)
        closes[symbol] = [(d, c) for (d, o, h, l, c, v) in rows]
        conn.executemany(
            "INSERT INTO ohlcv_daily (symbol, trade_date, open, high, low, close, adj_close, volume, source, ingested_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(symbol, d, o, h, l, c, c, v, SOURCE, NOW) for (d, o, h, l, c, v) in rows],
        )
    return closes


def seed_vix(conn) -> None:
    rng = random.Random(99)
    rows = []
    for d in business_days(TODAY, 90):
        base = 15 + 4 * math.sin(d.toordinal() / 9) + rng.gauss(0, 1.2)
        for tenor, adj in ((9, -0.4), (30, 0.0), (60, 0.6), (90, 1.1)):
            rows.append((d, tenor, round(max(9.0, base + adj), 2), SOURCE, NOW))
    conn.executemany(
        "INSERT INTO vix_term_structure_daily (trade_date, tenor_days, vix_value, source, ingested_at) VALUES (?, ?, ?, ?, ?)",
        rows,
    )


def _proba_for(direction: str, conviction: float) -> tuple[float, float, float]:
    rest = (1 - conviction) / 2
    if direction == "up":
        return (rest, rest, conviction)
    if direction == "down":
        return (conviction, rest, rest)
    return (rest, conviction, rest)


FEATURE_TEMPLATES = [
    "trend_strength_adx",
    "realized_vol_20d",
    "rsi_14",
    "put_call_ratio_z",
    "iv_rank_252",
    "sector_dispersion_z",
]


def seed_daily_predictions(conn, closes) -> None:
    rows = []
    snap_rows = []
    for symbol, (sector, base, drift, vol) in UNIVERSE.items():
        px = closes[symbol]
        # a per-symbol "skill" so backtest numbers differ but stay honest (~0.4-0.52)
        acc = round(RNG.uniform(0.41, 0.54), 3)
        # Iterate over the actual trading-day indices in the (possibly real,
        # holiday-aware) price series -- the last 12 sessions.
        pred_indices = list(range(max(0, len(px) - 12), len(px)))
        for idx in pred_indices:
            d, close_on = px[idx]
            is_today = idx == len(px) - 1
            # bias direction by drift so it looks coherent
            direction = RNG.choices(
                DIRECTIONS,
                weights=(1.4 if drift < 0 else 0.8, 1.0, 1.4 if drift > 0 else 0.8),
            )[0]
            conviction = round(RNG.uniform(0.42, 0.78), 3)
            pd_, pr_, pu_ = _proba_for(direction, conviction)
            regime = {"up": 1, "down": 2, "range": 0}[direction]
            if RNG.random() < 0.2:  # some noise in regime labelling
                regime = RNG.choice([0, 1, 2])
            band = close_on * vol * 1.6
            fv = {k: round(RNG.uniform(-2, 2), 4) for k in FEATURE_TEMPLATES}
            fv["realized_vol_20d"] = round(vol * math.sqrt(252), 4)
            pid = f"dp-{symbol}-{d.isoformat()}"
            if is_today:
                status, actual_price, actual_return, actual_label, outcome, graded_at = (
                    "pending", None, None, None, None, None,
                )
            else:
                # grade against the realised close `horizon` sessions later
                fut = min(len(px) - 1, idx + 5)
                actual_price = px[fut][1]
                actual_return = round((actual_price - close_on) / close_on, 4)
                if actual_return > band / close_on:
                    actual_label = "up"
                elif actual_return < -band / close_on:
                    actual_label = "down"
                else:
                    actual_label = "range"
                outcome = "win" if actual_label == direction else "loss"
                graded_at = NOW
                status = "graded"
            rows.append((
                pid, d, symbol, sector, 5, d + timedelta(days=7), regime,
                pd_, pr_, pu_, direction, close_on, 1.0,
                round(close_on + band, 2), round(close_on - band, 2),
                json.dumps(fv), "gmm-logistic-v2", status,
                actual_price, actual_return, actual_label, outcome, graded_at, NOW,
            ))
        # backtest snapshot per symbol
        n = RNG.randint(220, 990)
        snap_rows.append((
            TODAY, symbol, sector, n, acc, round(RNG.uniform(0.21, 0.26), 4),
            round(RNG.uniform(-0.2, 0.9), 3),
            RNG.randint(40, 120), round(RNG.uniform(0.45, 0.62), 3),
            RNG.randint(80, 200), round(RNG.uniform(0.33, 0.5), 3),
            round(RNG.uniform(-0.01, 0.04), 4), NOW,
        ))
    conn.executemany(
        """INSERT INTO daily_predictions
        (prediction_id, trade_date, symbol, sector, horizon, label_end_date, regime,
         proba_down, proba_range, proba_up, predicted_direction, entry_price, band_k,
         target_price_up, target_price_down, feature_values, model_version, status,
         actual_price, actual_return, actual_label, outcome, graded_at, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.executemany(
        """INSERT INTO symbol_backtest_snapshot
        (as_of_date, symbol, sector, overall_n, overall_accuracy, overall_brier, overall_sharpe,
         ev_passed_n, ev_passed_win_rate, ev_blocked_n, ev_blocked_win_rate, ev_of_continuing_now, computed_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        snap_rows,
    )


# --- Traders, league predictions, portfolios --------------------------------

EXTRA_TRADERS = [
    ("momentum", "Momentum (動能派)",
     "Trend & breakout: when ADX is high and price clears resistance, buy short-dated OTM calls in the direction of the move.",
     "momentum"),
    ("vol_seller", "Vol Seller (賣波動派)",
     "Sell overpriced premium: when IV rank is high and the tape is ranging, sell puts / spreads and take profit at 50-80% of max.",
     "vol_seller"),
    ("contrarian", "Contrarian (逆勢派)",
     "Mean reversion at extremes: when put/call and RSI hit extremes, fade the crowd with short-dated reversal options.",
     "contrarian"),
    ("macro", "Macro (總經派)",
     "Top-down cross-asset: read rates/DXY/oil regime, then express the sector view through ETF options (SOXL/SOXS).",
     "macro"),
]

# trader_id -> (skill win-rate bias, dollar edge per trade) -- keeps the league
# differentiated but realistic (no one crushes; some are underwater).
TRADER_SKILL = {
    "chartist": (0.52, 60),
    "analyst": (0.49, 20),
    "momentum": (0.55, 140),
    "vol_seller": (0.58, 90),
    "contrarian": (0.46, -40),
    "macro": (0.50, 30),
}

RATIONALES = {
    "up": [
        "站上 20 日均線且 ADX 走揚,動能轉強。",
        "IV 便宜、消息面偏多,買方風險報酬佳。",
        "跌深後量能背離,短線反彈。",
    ],
    "down": [
        "跌破頸線、相對強弱轉弱,順勢偏空。",
        "skew 陡峭、資金流出,尾部風險升高。",
        "漲多過熱、put/call 極端,short-dated 反轉。",
    ],
    "range": [
        "區間盤整、IV rank 偏高,適合賣方收租。",
        "方向不明,等待催化劑,先不進場。",
        "gamma 為正、盤面被壓平,波動收斂。",
    ],
}
INVALIDATIONS = [
    "跌破前低 / regime 由多翻空",
    "站回關鍵均線 / 消息轉向",
    "IV 崩跌、波動率溢價消失",
    "到期前未達目標價區間",
]


def seed_traders(conn) -> None:
    conn.executemany(
        "INSERT INTO traders (trader_id, name, philosophy, engine_key, active, added_date, created_at) "
        "VALUES (?, ?, ?, ?, true, ?, ?)",
        [(tid, name, phil, key, date(2026, 7, 11), NOW) for (tid, name, phil, key) in EXTRA_TRADERS],
    )
    conn.executemany(
        "INSERT INTO trader_method_versions (trader_id, method_version, effective_date, spec, status, created_at) "
        "VALUES (?, ?, ?, ?, 'active', ?)",
        [(tid, f"{tid}:v1", date(2026, 7, 11), phil, NOW) for (tid, name, phil, key) in EXTRA_TRADERS],
    )


def seed_trader_predictions(conn, closes) -> None:
    all_traders = ["chartist", "analyst"] + [t[0] for t in EXTRA_TRADERS]
    symbols = list(UNIVERSE)
    # Per-symbol date -> (index, close), and a canonical trading calendar taken
    # from the longest series (all US equities share the same sessions).
    idx_by_date = {s: {d: (i, c) for i, (d, c) in enumerate(closes[s])} for s in symbols}
    reference = max(symbols, key=lambda s: len(closes[s]))
    calendar = [d for (d, _) in closes[reference]]
    today_d = calendar[-1]
    rows = []
    for tid in all_traders:
        win_bias, _ = TRADER_SKILL[tid]
        method = "chartist:gmm-logistic-v2" if tid == "chartist" else (
            "analyst:catalyst-v1" if tid == "analyst" else f"{tid}:v1")
        # each trader covers 6 symbols across the last 8 sessions
        covered = RNG.sample(symbols, 6)
        pred_days = calendar[-8:]
        for d in pred_days:
            is_today = d == today_d
            for symbol in covered:
                if RNG.random() < 0.35:
                    continue  # traders skip when they have no edge
                sector, base, drift, vol = UNIVERSE[symbol]
                px = closes[symbol]
                if d not in idx_by_date[symbol]:
                    continue  # symbol wasn't trading that session
                idx, close_on = idx_by_date[symbol][d]
                band = close_on * vol * 1.6
                pid = f"tp-{tid}-{symbol}-{d.isoformat()}"
                if is_today:
                    direction = RNG.choices(DIRECTIONS, weights=(1, 0.8, 1.2))[0]
                    conviction = round(RNG.uniform(0.45, 0.82), 3)
                    regime = {"up": 1, "down": 2, "range": 0}[direction]
                    payload = {"conviction": conviction, "engine": method}
                    status, ap, ar, al, outcome, graded_at = "pending", None, None, None, None, None
                else:
                    # Grade against the real seeded price (a market fact). SKILL is
                    # modelled as the trader picking the direction that matched the
                    # realised move more often -- synthetic demo only, never used by
                    # any model-training path.
                    idx = [j for j, (dd, _) in enumerate(px) if dd == d][0]
                    fut = min(len(px) - 1, idx + 5)
                    ap = px[fut][1]
                    ar = round((ap - close_on) / close_on, 4)
                    al = "up" if ar > band / close_on else ("down" if ar < -band / close_on else "range")
                    if RNG.random() < win_bias:
                        direction = al  # skilled call: matched the realised label
                    else:
                        direction = RNG.choice([x for x in DIRECTIONS if x != al])
                    conviction = round(RNG.uniform(0.62, 0.85) if direction == al else RNG.uniform(0.45, 0.66), 3)
                    regime = {"up": 1, "down": 2, "range": 0}[direction]
                    payload = {"conviction": conviction, "engine": method}
                    outcome = "win" if direction == al else "loss"
                    graded_at, status = NOW, "graded"
                rows.append((
                    pid, tid, method, d, symbol, sector, 5, d + timedelta(days=7),
                    direction, conviction, RNG.choice(RATIONALES[direction]), RNG.choice(INVALIDATIONS),
                    regime, close_on, 1.0, round(vol * math.sqrt(252), 4), json.dumps(payload),
                    NOW, status, ap, ar, al, outcome, graded_at, NOW,
                ))
    conn.executemany(
        """INSERT INTO trader_predictions
        (prediction_id, trader_id, method_version, trade_date, symbol, sector, horizon, label_end_date,
         direction, conviction, rationale, invalidation, regime, entry_price, band_k, grade_vol,
         engine_payload, available_at, status, actual_price, actual_return, actual_label, outcome,
         graded_at, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )


def seed_portfolios(conn, closes) -> None:
    all_traders = ["chartist", "analyst"] + [t[0] for t in EXTRA_TRADERS]
    START = 25000.0
    port_rows = []
    trade_rows = []
    for tid in all_traders:
        win_bias, edge = TRADER_SKILL[tid]
        cash = START
        rng = random.Random(hash((tid, "port")) & 0xFFFFFFFF)
        symbols = rng.sample(list(UNIVERSE), 5)
        # 8-12 closed trades over the last ~45 days; win count tracks skill so
        # the contest leaderboard is differentiated but believable.
        n_closed = rng.randint(8, 12)
        outcomes = [True] * round(n_closed * win_bias) + [False] * (n_closed - round(n_closed * win_bias))
        rng.shuffle(outcomes)
        for win in outcomes:
            symbol = rng.choice(symbols)
            sector, base, drift, vol = UNIVERSE[symbol]
            px = closes[symbol]
            entry_i = rng.randint(45, 80)
            exit_i = min(len(px) - 1, entry_i + rng.randint(3, 12))
            entry_d, entry_px = px[entry_i]
            exit_d, exit_px = px[exit_i]
            right = "call" if tid != "contrarian" else rng.choice(["call", "put"])
            side = "short" if tid == "vol_seller" else "long"
            strike = round(entry_px * (1.03 if right == "call" else 0.97), 1)
            contracts = rng.randint(1, 2)
            entry_prem = round(entry_px * rng.uniform(0.02, 0.04), 2)
            basis = entry_prem * 100 * contracts  # premium paid (long) or collected (short)
            # Magnitudes are tuned so win-RATE (skill) drives net return: winners
            # and losers are of similar size, so a coin-flip trader nets ~flat and
            # skill separates the field into winners and losers.
            if side == "long":
                pnl = basis * rng.uniform(0.45, 0.95) if win else -basis * rng.uniform(0.55, 1.0)
                exit_prem = max(0.01, round(entry_prem + pnl / (100 * contracts), 2))
            else:
                # seller: take profit at 50-80% of the credit; losses run larger
                pnl = basis * rng.uniform(0.4, 0.7) if win else -basis * rng.uniform(0.8, 1.6)
                exit_prem = max(0.01, round(entry_prem - pnl / (100 * contracts), 2))
            pnl = round(pnl, 2)
            cash += pnl  # round-trip P&L; open-trade capital is debited separately below
            reason = ("達停利目標" if side == "long" else "回補 50-80% 利潤") if win else \
                rng.choice(["觸發停損", "邏輯失效", "到期歸零"])
            trade_rows.append((
                str(uuid.uuid4()), tid, symbol, right, side, strike,
                exit_d + timedelta(days=rng.randint(1, 20)), contracts,
                datetime.combine(entry_d, datetime.min.time(), timezone.utc), entry_px, entry_prem,
                datetime.combine(exit_d, datetime.min.time(), timezone.utc), exit_px, exit_prem, pnl,
                "closed", rng.choice(RATIONALES["up" if right == "call" else "down"]), reason, None, NOW,
            ))
        # 1-3 open trades = current holdings
        for _ in range(rng.randint(1, 3)):
            symbol = rng.choice(symbols)
            sector, base, drift, vol = UNIVERSE[symbol]
            px = closes[symbol]
            entry_i = rng.randint(len(px) - 12, len(px) - 2)
            entry_d, entry_px = px[entry_i]
            right = "call" if rng.random() < 0.7 else "put"
            side = "short" if tid == "vol_seller" and rng.random() < 0.6 else "long"
            strike = round(entry_px * (1.03 if right == "call" else 0.97), 1)
            contracts = 1
            entry_prem = round(entry_px * rng.uniform(0.02, 0.05), 2)
            cost = entry_prem * 100 * contracts
            cash -= cost if side == "long" else -cost  # short collects
            trade_rows.append((
                str(uuid.uuid4()), tid, symbol, right, side, strike,
                TODAY + timedelta(days=rng.randint(10, 35)), contracts,
                datetime.combine(entry_d, datetime.min.time(), timezone.utc), entry_px, entry_prem,
                None, None, None, None, "open",
                rng.choice(RATIONALES["up" if right == "call" else "down"]), None, None, NOW,
            ))
        port_rows.append((tid, START, round(cash, 2), 0.20, "short_dated_calls_puts", date(2026, 5, 26), NOW))
    conn.executemany(
        "INSERT INTO trader_portfolios (trader_id, starting_capital, cash, max_position_pct, instrument_scope, inception_date, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        port_rows,
    )
    conn.executemany(
        """INSERT INTO trader_trades
        (trade_id, trader_id, symbol, option_right, side, strike, expiry_date, contracts,
         entry_at, entry_underlying, entry_premium, exit_at, exit_underlying, exit_premium,
         realized_pnl, status, thesis, exit_reason, linked_prediction_id, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        trade_rows,
    )


# --- News, catalysts, events, human option positions ------------------------

NEWS = [
    ("NVDA", "analyst_rating", "Morgan Stanley 重申 NVDA 加碼,目標價上調至 $210",
     "大摩認為資料中心需求能見度延伸至 2027 年,上調目標價並維持首選。", "Morgan Stanley", 0.7, 0.85, 0.6, 0.4),
    ("NVDA", "earnings", "NVDA 財報前:市場預期 Blackwell 出貨帶動毛利率走高",
     "分析師平均預估營收 QoQ +9%,重點在資料中心指引與供給瓶頸緩解。", "Bloomberg", 0.4, 0.8, 0.5, 0.55),
    ("AVGO", "catalyst", "AVGO 客製化 ASIC 訂單傳擴大,雲端資本支出外溢",
     "供應鏈訊號顯示第三家超大規模客戶投片,若屬實將墊高明年營收基期。", None, 0.6, 0.7, 0.8, 0.3),
    ("AMD", "analyst_rating", "AMD 遭降評至中立:MI 系列競爭加劇、估值偏高",
     "賣方擔憂 AI 加速器價格戰壓縮毛利,短線評價面臨修正。", "Goldman Sachs", -0.55, 0.6, 0.5, 0.45),
    ("TSM", "earnings", "TSM 月營收年增 38%,先進製程滿載",
     "3nm/5nm 產能利用率維持高檔,法說會聚焦 CoWoS 擴產進度。", "Reuters", 0.6, 0.65, 0.4, 0.6),
    ("AAPL", "headline", "傳 Apple 加速 AI 伺服器自研晶片,2027 上線",
     "報導稱 Apple 與台積電合作開發資料中心晶片,強化 Apple Intelligence 後端。", "The Information", 0.45, 0.55, 0.7, 0.35),
    ("MSFT", "catalyst", "MSFT Azure AI 需求超前部署,資本支出再上修",
     "通路調查顯示 GPU 租賃排隊時間拉長,雲端毛利短期承壓但成長確定性高。", None, 0.5, 0.6, 0.75, 0.4),
    ("GOOGL", "analyst_rating", "GOOGL 獲上調:雲端轉盈 + 廣告韌性",
     "多家券商上修雲端利潤率假設,認為 Gemini 變現進度優於預期。", "UBS", 0.6, 0.5, 0.55, 0.5),
    ("META", "headline", "Meta 重整 AI 團隊,加碼超級智慧實驗室",
     "祖克柏親自督軍新 AI 部門,市場關注投資強度與變現時程。", "WSJ", 0.2, 0.5, 0.6, 0.45),
    ("AMZN", "earnings", "AMZN 電商旺季前置備貨,AWS 重回雙位數成長",
     "分析師預期 AWS 加速,零售利潤率受物流投資拖累,指引為關鍵。", "CNBC", 0.35, 0.55, 0.45, 0.5),
    (None, "macro", "Fed 官員談話偏鷹,9 月降息機率降至 55%",
     "多位票委強調通膨黏性,利率期貨定價回吐,科技股評價面承壓。", "FRED / Fed", -0.4, 0.9, 0.7, 0.5),
    (None, "macro", "6 月 CPI 年增 2.9%,略低於市場預期",
     "核心服務通膨降溫,支持軟著陸情境,風險性資產受激勵。", "BLS", 0.5, 0.85, 0.65, 0.55),
    (None, "macro", "10 年期公債殖利率回落至 4.15%,DXY 走弱",
     "殖利率與美元同步走低,對高評價成長股構成順風。", "FRED", 0.4, 0.7, 0.5, 0.5),
    ("SOXL", "catalyst", "費半資金流連三日淨流入,槓桿 ETF 動能增溫",
     "SOXL 出現連續申購,顯示資金押注半導體反彈,但槓桿衰減風險需留意。", None, 0.5, 0.6, 0.7, 0.4),
    ("NVDA", "headline", "傳主權基金加碼 AI 基建,NVDA 為主要受益者",
     "中東主權基金規劃大型 AI 資料中心採購,長線需求敘事延續。", "Financial Times", 0.55, 0.6, 0.6, 0.4),
]


def seed_news_and_events(conn, closes) -> None:
    news_rows = []
    catalyst_rows = []
    for i, (symbol, itype, headline, summary, src, sent, imp, nov, priced) in enumerate(NEWS):
        published = NOW - timedelta(hours=RNG.randint(2, 60))
        item_id = f"news-{i:03d}"
        chain = None
        refs = json.dumps([f"raw-{i}-{j}" for j in range(RNG.randint(1, 3))])
        if itype == "catalyst":
            chain = f"{summary} → 相關供應鏈/客戶 → {symbol or '板塊'} 評價重估"
            # dual-write to legacy catalyst_signals so per-ticker catalyst panel stays populated
            catalyst_rows.append((
                f"cat-{i:03d}", symbol, TODAY, headline, chain, nov, sent, priced, refs,
                "catalyst-synth-v1", published, NOW,
            ))
        news_rows.append((
            item_id, symbol, itype, headline, summary, f"https://example.com/news/{item_id}",
            src, published, sent, imp, nov, priced, chain, refs, published, NOW,
        ))
    conn.executemany(
        """INSERT INTO news_items
        (item_id, symbol, item_type, headline, summary, url, source_name, published_at,
         sentiment_score, importance, novelty_score, priced_in_estimate, transmission_chain,
         source_refs, available_at, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        news_rows,
    )
    if catalyst_rows:
        conn.executemany(
            """INSERT INTO catalyst_signals
            (signal_id, symbol, as_of_date, catalyst_summary, transmission_chain, novelty_score,
             sentiment_score, priced_in_estimate, source_refs, model_version, available_at, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            catalyst_rows,
        )


def seed_events(conn) -> None:
    events = [
        ("NVDA", "earnings", NOW + timedelta(days=6)),
        ("AAPL", "earnings", NOW + timedelta(days=12)),
        ("AMD", "earnings", NOW + timedelta(days=9)),
        ("MSFT", "earnings", NOW + timedelta(days=14)),
        ("GOOGL", "earnings", NOW + timedelta(days=15)),
        (None, "fomc", NOW + timedelta(days=8)),
        (None, "cpi", NOW + timedelta(days=3)),
        (None, "nfp", NOW + timedelta(days=25)),
    ]
    rows = [
        (f"evt-{i:02d}", sym, etype, when, "scheduled", SOURCE, NOW)
        for i, (sym, etype, when) in enumerate(events)
    ]
    conn.executemany(
        "INSERT INTO event_calendar (event_id, symbol, event_type, scheduled_at, status, source, ingested_at) "
        "VALUES (?,?,?,?,?,?,?)",
        rows,
    )


def seed_human_positions(conn, closes) -> None:
    rows = []
    picks = [("NVDA", "call", "long"), ("SOXL", "call", "long"), ("AMD", "put", "long")]
    for i, (symbol, right, side) in enumerate(picks):
        px = closes[symbol]
        entry_d, entry_px = px[-9]
        strike = round(entry_px * (1.03 if right == "call" else 0.97), 1)
        rows.append((
            f"pos-{i}", symbol, right, side, strike, TODAY + timedelta(days=21),
            entry_d, entry_px, round(entry_px * 0.035, 2), round(0.30 * math.sqrt(252) / math.sqrt(252) + 0.30, 3),
            1, "示範持倉:demo seed", "open", None, None, NOW,
        ))
    conn.executemany(
        """INSERT INTO option_positions
        (position_id, symbol, option_right, side, strike, expiry_date, entry_date,
         entry_underlying_price, entry_premium, entry_iv, regime_at_entry, thesis_note,
         status, closed_at, closed_reason, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )


def seed_ingestion_runs(conn) -> None:
    tables = ["ohlcv_daily", "macro_series_daily", "iv_surface_daily", "news_articles_raw",
              "social_posts_raw", "event_news_gdelt"]
    rows = [
        (f"run-{t}", SOURCE, t, TODAY - timedelta(days=90), TODAY, RNG.randint(100, 5000),
         "success", None, NOW - timedelta(hours=RNG.randint(1, 20)), NOW)
        for t in tables
    ]
    conn.executemany(
        "INSERT INTO ingestion_runs (run_id, source, target_table, window_start, window_end, rows_written, status, error_message, started_at, finished_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--synthetic", action="store_true",
                        help="force the synthetic random-walk prices instead of live yfinance")
    args = parser.parse_args()

    conn = get_connection(args.db)
    run_migrations(conn)

    # wipe any prior demo rows so re-running is idempotent
    for tbl in ("ohlcv_daily", "vix_term_structure_daily", "daily_predictions",
                "symbol_backtest_snapshot", "trader_predictions", "trader_portfolios",
                "trader_trades", "news_items", "catalyst_signals", "event_calendar",
                "option_positions", "ingestion_runs"):
        conn.execute(f"DELETE FROM {tbl}")
    # drop the migration-seeded extra traders/methods so we can reseed cleanly
    conn.execute("DELETE FROM trader_method_versions WHERE trader_id NOT IN ('chartist','analyst')")
    conn.execute("DELETE FROM traders WHERE trader_id NOT IN ('chartist','analyst')")

    closes = seed_prices(conn, use_real=not args.synthetic)
    seed_vix(conn)
    seed_daily_predictions(conn, closes)
    seed_traders(conn)
    seed_trader_predictions(conn, closes)
    seed_portfolios(conn, closes)
    seed_news_and_events(conn, closes)
    seed_events(conn)
    seed_human_positions(conn, closes)
    seed_ingestion_runs(conn)

    counts = {
        t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in ("ohlcv_daily", "daily_predictions", "symbol_backtest_snapshot",
                  "traders", "trader_predictions", "trader_portfolios", "trader_trades",
                  "news_items", "event_calendar", "option_positions")
    }
    conn.close()
    print("seeded:", json.dumps(counts, indent=2))


if __name__ == "__main__":
    main()
