"""Run every Phase 1 sleeve test and the combined-portfolio simulation.

    cd research/phase1 && ../../.venv/bin/python run_all.py

Writes markdown tables to research/phase1/results/. Research only: no broker, no orders.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

import common as C
import crypto
import desk_book
import letf
import meanrev
from stockmoney.desk.universe import ALL_SYMBOLS, STOCKS

COLS = ["name", "period", "CAGR", "MaxDD", "Sharpe", "Sortino", "Vol", "TimeIn", "Trades",
        "Win%", "PF", "AvgTrade", "AvgTrade_lo", "AvgTrade_hi", "WorstYr", "WorstYrLbl"]
BCOLS = ["name", "period", "CAGR", "MaxDD", "Sharpe", "Sortino", "Vol", "WorstYr", "WorstYrLbl"]
OUT: dict[str, str] = {}
KEY: dict = {}


def row(name, ret, start, end, ann=252, pos=None, trades=None, label=None):
    r = ret.loc[start:end]
    if len(r) < 30:
        return None
    t = None
    if trades is not None:
        t = trades
        if len(t):
            ed = pd.to_datetime(t["entry_date"])
            t = t[(ed >= r.index[0]) & (ed <= r.index[-1])]
    p = pos.loc[r.index[0]:r.index[-1]] if pos is not None else None
    s = C.stats(r, ann=ann, pos=p, trades=t)
    return {"name": name, "period": label or f"{r.index[0]:%Y-%m}~{r.index[-1]:%Y-%m}", **s}


def drawdowns(ret: pd.Series, k: int = 3) -> list[dict]:
    eq = (1 + ret).cumprod()
    dd = eq / eq.cummax() - 1
    out, used = [], pd.Series(False, index=dd.index)
    for _ in range(k):
        d = dd[~used]
        if d.empty or d.min() >= -0.01:
            break
        trough = d.idxmin()
        peak = eq.loc[:trough].idxmax()
        rec = eq.loc[trough:][eq.loc[trough:] >= eq.loc[peak]]
        end = rec.index[0] if len(rec) else None
        out.append({"peak": peak.date(), "trough": trough.date(), "depth": dd.loc[trough],
                    "recovered": end.date() if end is not None else "未回復"})
        used.loc[peak:(end or dd.index[-1])] = True
    return out


def dd_table(name: str, ret: pd.Series) -> str:
    lines = [f"**{name}**", "", "| 高點 | 谷底 | 深度 | 回到前高 |", "|---|---|---|---|"]
    for d in drawdowns(ret):
        lines.append(f"| {d['peak']} | {d['trough']} | {d['depth'] * 100:.1f}% | {d['recovered']} |")
    return "\n".join(lines)


def boot_rows(pairs: list[tuple[str, pd.Series, pd.Series, str]], ann=252) -> str:
    lines = ["| 策略 | 對照 | 期間 | Sharpe 差 [95% CI] | 年化對數成長差 [95% CI] |", "|---|---|---|---|---|"]
    for name, a, b, bname in pairs:
        both = pd.concat([a, b], axis=1).dropna()
        s = C.block_bootstrap_sharpe_diff(both.iloc[:, 0], both.iloc[:, 1], ann=ann)
        g = C.block_bootstrap_cagr_diff(both.iloc[:, 0], both.iloc[:, 1], ann=ann)
        star = lambda x: "*" if x[1] > 0 or x[2] < 0 else ""
        lines.append(
            f"| {name} | {bname} | {both.index[0]:%Y-%m}~{both.index[-1]:%Y-%m} | "
            f"{s[0]:+.2f} [{s[1]:+.2f}, {s[2]:+.2f}]{star(s)} | "
            f"{g[0] * 100:+.1f}% [{g[1] * 100:+.1f}%, {g[2] * 100:+.1f}%]{star(g)} |")
    return "\n".join(lines)


# =========================================================================== sleeves
def section_letf(core):
    o = letf.run(core)
    periods = [("2010-02-01", "2018-12-31", "2010-2018"), ("2019-01-01", None, "2019-今"), (None, None, "全期")]
    rows, brows = [], []
    for k, r in o["real"].items():
        for s, e, lbl in periods:
            rows.append(row(k, r, s, e, pos=o["pos"][k], trades=o["trades"][k], label=lbl))
    real_idx = o["real"]["TQQQ_sma200"].index
    for b in ("QQQ_BH", "TQQQ_BH", "SOXX_BH", "SOXL_BH"):
        for s, e, lbl in periods:
            brows.append(row(b, o["bench"][b], s, e, label=lbl))
    OUT["letf_real"] = C.fmt_table([r for r in rows if r], COLS) + "\n\n基準(買進持有):\n\n" + C.fmt_table([r for r in brows if r], BCOLS)

    speriods = [("1986-01-01", "1999-12-31", "1986-1999"), ("2000-01-01", "2009-12-31", "2000-2009"),
                ("2010-01-01", "2018-12-31", "2010-2018"), ("2019-01-01", None, "2019-今"), (None, None, "全期")]
    rows, brows = [], []
    for k, r in o["synth"].items():
        for s, e, lbl in speriods:
            rows.append(row(k, r, s, e, pos=o["pos"][k], trades=o["trades"][k], label=lbl))
    for b in ("^NDX_BH", "TQQQsyn_BH", "^SOX_BH", "SOXLsyn_BH"):
        for s, e, lbl in speriods:
            brows.append(row(b, o["bench"][b], s, e, label=lbl))
    OUT["letf_synth"] = C.fmt_table([r for r in rows if r], COLS) + "\n\n基準(買進持有,合成 3x 與指數本身):\n\n" + C.fmt_table([r for r in brows if r], BCOLS)
    OUT["letf_tracking"] = "\n".join(
        ["| 標的 | 起 | 真實 ETF 年化 | 合成年化 | 日報酬相關 |", "|---|---|---|---|---|"]
        + [f"| {k} | {v['start']} | {v['real_CAGR'] * 100:.1f}% | {v['syn_CAGR'] * 100:.1f}% | {v['daily_corr']:.4f} |"
           for k, v in o["tracking"].items()])
    pairs = []
    for k in o["real"]:
        und = "QQQ_BH" if k.startswith("TQQQ") else "SOXX_BH"
        etf = "TQQQ_BH" if k.startswith("TQQQ") else "SOXL_BH"
        pairs += [(k, o["real"][k], o["bench"][und], und), (k, o["real"][k], o["bench"][etf], etf)]
    for k in o["synth"]:
        und = "^NDX_BH" if k.startswith("TQQQ") else "^SOX_BH"
        pairs.append((k, o["synth"][k].loc["2000":], o["bench"][und].loc["2000":], und + "(2000起)"))
    pairs.append(("TQQQ_sma200_b2_vol", o["real"]["TQQQ_sma200_b2_vol"], o["real"]["TQQQ_sma200_b2"], "TQQQ_sma200_b2"))
    pairs.append(("TQQQ_sma200_b2", o["real"]["TQQQ_sma200_b2"], o["real"]["TQQQ_sma200"], "TQQQ_sma200"))
    OUT["letf_boot"] = boot_rows(pairs)
    OUT["letf_dd"] = "\n\n".join([
        dd_table("TQQQ_sma200_b2(真實 ETF)", o["real"]["TQQQ_sma200_b2"]),
        dd_table("TQQQsyn_sma200_b2(合成,1986 起)", o["synth"]["TQQQsyn_sma200_b2"]),
        dd_table("SOXL_sma200_b2(真實 ETF)", o["real"]["SOXL_sma200_b2"]),
        dd_table("SOXLsyn_sma200_b2(合成,1995 起)", o["synth"]["SOXLsyn_sma200_b2"]),
    ])
    return o


def section_meanrev(core, stocks_long):
    periods = [("2000-01-01", "2009-12-31", "2000-2009"), ("2010-01-01", "2018-12-31", "2010-2018"),
               ("2019-01-01", None, "2019-今"), ("2000-01-01", None, "2000-今")]
    rows, brows, pairs = [], [], []
    res = {}
    for slip in (0.0005, 0.0010):
        o = meanrev.run_index(core, slip)
        for k, r in o["ret"].items():
            name = f"{k}@{int(slip * 1e4)}bp"
            res[name] = r
            for s, e, lbl in periods:
                rows.append(row(name, r, s, e, pos=o["pos"][k], trades=o["trades"][k], label=lbl))
            if slip == 0.0005:
                b = "SPY_BH" if k.startswith("SPY") else "QQQ_BH"
                pairs.append((name, r.loc["2000":], o["bench"][b].loc["2000":], b))
    for b in ("SPY_BH", "QQQ_BH"):
        for s, e, lbl in periods:
            brows.append(row(b, o["bench"][b], s, e, label=lbl))
    OUT["mr_index"] = C.fmt_table([r for r in rows if r], COLS) + "\n\n基準:\n\n" + C.fmt_table([r for r in brows if r], BCOLS)

    close = C.wide(core, "close")
    stk = stocks_long[stocks_long.symbol.isin(list(STOCKS))]
    sect = stocks_long[stocks_long.symbol.isin(["XLK", "XLY", "XLP", "XLF", "XLV", "XLI", "XLE", "XLB", "XLU", "SMH"])]
    bperiods = [("2005-01-01", "2009-12-31", "2005-2009"), ("2010-01-01", "2018-12-31", "2010-2018"),
                ("2019-01-01", None, "2019-今"), ("2005-01-01", None, "2005-今")]
    rows = []
    spy = close["SPY"].dropna().pct_change()
    for uname, uni in (("stocks", stk), ("sectors", sect)):
        cash = C.cash_returns(C.wide(uni, "close").index, close["^IRX"], close["BIL"])
        for v in ("rsi2", "down3"):
            for slip in (0.0005, 0.0010):
                r = meanrev.run_basket(uni, cash, v, slip)
                name = f"basket_{uname}_{v}@{int(slip * 1e4)}bp"
                res[name] = r.ret
                for s, e, lbl in bperiods:
                    rows.append(row(name, r.ret, s, e, pos=r.pos, trades=r.trades, label=lbl))
                if slip == 0.0005:
                    pairs.append((name, r.ret.loc["2005":], spy.reindex(r.ret.index).loc["2005":], "SPY_BH"))
    ew = C.wide(stk, "close").pct_change().mean(axis=1).fillna(0)
    brows = [row("全清單等權持有(倖存者)", ew, s, e, label=lbl) for s, e, lbl in bperiods]
    brows += [row("SPY_BH", spy.fillna(0), s, e, label=lbl) for s, e, lbl in bperiods]
    OUT["mr_basket"] = C.fmt_table([r for r in rows if r], COLS) + "\n\n基準:\n\n" + C.fmt_table([r for r in brows if r], BCOLS)
    OUT["mr_boot"] = boot_rows(pairs)
    return res


def section_crypto(core):
    o = crypto.run(core)
    periods = [("2015-01-01", "2018-12-31", "2015-2018"), ("2019-01-01", None, "2019-今"), ("2015-01-01", None, "全期")]
    rows, brows, pairs = [], [], []
    for k, r in o["ret"].items():
        for s, e, lbl in periods:
            rows.append(row(k, r, s, e, ann=365, pos=o["pos"][k], trades=o["trades"][k], label=lbl))
        b = k.split("_")[0] + "_BH"
        start = "2015" if k.startswith("BTC") else "2018-03"
        pairs.append((k, r.loc[start:], o["bench"][b].loc[start:], b))
    for b in ("BTC_BH", "ETH_BH"):
        for s, e, lbl in periods:
            brows.append(row(b, o["bench"][b], s, e, ann=365, label=lbl))
    OUT["crypto"] = C.fmt_table([r for r in rows if r], COLS) + "\n\n基準:\n\n" + C.fmt_table([r for r in brows if r], BCOLS)
    OUT["crypto_boot"] = boot_rows(pairs, ann=365)
    OUT["crypto_dd"] = "\n\n".join([dd_table("BTC_sma100", o["ret"]["BTC_sma100"].loc["2015":]),
                                    dd_table("BTC_donch55_20", o["ret"]["BTC_donch55_20"].loc["2015":])])
    return o


def section_desk():
    out = {}
    rows = []
    for which in ("standard", "long"):
        d = desk_book.run(which)
        out[which] = d
        t = d["trades"]
        wins, losses = t.loc[t.pnl > 0, "pnl"].sum(), -t.loc[t.pnl <= 0, "pnl"].sum()
        for s, e, lbl in ([("2015-06-01", "2018-12-31", "2015-06~2018"), ("2019-01-01", None, "2019-今"), (None, None, "全期")]
                          if which == "standard" else
                          [("2005-01-01", "2009-12-31", "2005-2009"), ("2010-01-01", "2018-12-31", "2010-2018"),
                           ("2019-01-01", None, "2019-今"), (None, None, "全期")]):
            r = row(f"desk_combined_{which}", d["ret"], s, e, trades=t, label=lbl)
            r["TimeIn"] = np.nan
            rows.append(r)
        rows[-1]["PF"] = wins / losses  # P&L-weighted, matches backtest.trade_stats
    OUT["desk"] = C.fmt_table(rows, COLS)
    return out


# =========================================================================== portfolio
def combine(sleeves: pd.DataFrame, weights_fn, cost: dict[str, float]) -> tuple[pd.Series, pd.DataFrame]:
    """Monthly rebalanced sub-accounts. weights_fn(t, history) -> target weights at the
    first trading day of each month, using only returns strictly before t."""
    idx = sleeves.index
    vals = None
    rets, wlog = np.zeros(len(idx)), []
    month = None
    R = sleeves.to_numpy()
    for i, t in enumerate(idx):
        if t.month != month:
            month = t.month
            w = weights_fn(t, sleeves.iloc[:i])
            total = vals.sum() if vals is not None else 1.0
            target = total * w
            c = 0.0 if vals is None else sum(abs(target[j] - vals[j]) * cost.get(col, 0.0005)
                                             for j, col in enumerate(sleeves.columns))
            vals = target * (1 - c / total)
            wlog.append({"date": t, **dict(zip(sleeves.columns, w))})
        prev = vals.sum()
        vals = vals * (1 + R[i])
        rets[i] = vals.sum() / prev - 1
    return pd.Series(rets, idx), pd.DataFrame(wlog).set_index("date")


def fixed(w: dict[str, float], cols: list[str]):
    v = np.array([w.get(c, 0.0) for c in cols])
    return lambda t, h: v


def inverse_vol(cols: list[str], lookback: int = 252, cap: float = 0.40):
    def f(t, h):
        h = h.iloc[-lookback:]
        if len(h) < 60:
            return np.full(len(cols), 1 / len(cols))
        vol = h.std().replace(0, np.nan).fillna(h.std().max()).to_numpy()
        w = (1 / vol) / (1 / vol).sum()
        for _ in range(10):  # cap and redistribute
            over = w > cap
            if not over.any():
                break
            excess = (w[over] - cap).sum()
            w[over] = cap
            w[~over] += excess * w[~over] / w[~over].sum()
        return w
    return f


def boot_maxdd(ret: pd.Series, block: int = 63, n_boot: int = 2000, seed: int = 1) -> tuple[float, float, float]:
    x = ret.dropna().to_numpy()
    n = len(x)
    rng = np.random.default_rng(seed)
    k = int(np.ceil(n / block))
    mdd = np.empty(n_boot)
    for i in range(n_boot):
        starts = rng.integers(0, n, k)
        ix = (starts[:, None] + np.arange(block)[None, :]).ravel()[:n] % n
        eq = np.cumprod(1 + x[ix])
        mdd[i] = (eq / np.maximum.accumulate(eq) - 1).min()
    return float(np.median(mdd)), float(np.percentile(mdd, 5)), float((mdd < -0.35).mean())


def section_portfolio(core, lo, mr, cr, desk):
    nyse = desk["standard"]["ret"].index
    close = C.wide(core, "close")
    cash = C.cash_returns(close.index, close["^IRX"], close["BIL"])
    btc = crypto.to_trading_days(cr["ret"]["BTC_sma100"], nyse)
    sl = pd.DataFrame({
        "desk": desk["standard"]["ret"],
        "tqqq": lo["real"]["TQQQ_sma200_b2"].reindex(nyse),
        "mr": mr["basket_stocks_rsi2@5bp"].reindex(nyse),
        "btc": btc,
        "soxl": lo["real"]["SOXL_sma200_b2"].reindex(nyse),
        "mr_spy": mr["SPY_rsi2@5bp"].reindex(nyse),
    }).fillna(0.0)
    cost = {"btc": 0.003, "soxl": 0.001}
    cols = ["desk", "tqqq", "mr", "btc"]
    allocs = {
        "A 固定風險預算 50/20/20/10": fixed({"desk": .5, "tqqq": .2, "mr": .2, "btc": .1}, cols),
        "B 逆波動(252日,上限40%)": inverse_vol(cols),
        "C 等權 25x4": fixed({c: .25 for c in cols}, cols),
        "D 只有 desk 書": fixed({"desk": 1.0}, cols),
    }
    rows, curves, weights = [], {}, {}
    spy = close["SPY"].dropna().pct_change().reindex(nyse).fillna(0)
    for name, fn in allocs.items():
        r, wl = combine(sl[cols], fn, cost)
        curves[name], weights[name] = r, wl
        for s, e, lbl in [("2015-06-01", "2018-12-31", "2015-06~2018"), ("2019-01-01", None, "2019-今"), (None, None, "全期")]:
            rows.append(row(name, r, s, e, label=lbl))
    for s, e, lbl in [("2015-06-01", "2018-12-31", "2015-06~2018"), ("2019-01-01", None, "2019-今"), (None, None, "全期")]:
        rows.append(row("SPY_BH", spy, s, e, label=lbl))
    OUT["port"] = C.fmt_table(rows, BCOLS)
    OUT["port_dd"] = "\n\n".join(dd_table(n, curves[n]) for n in list(allocs)[:3])
    corr = sl.corr()
    OUT["port_corr_daily"] = C.df_md(corr)
    m = (1 + sl).resample("ME").prod() - 1
    OUT["port_corr_monthly"] = C.df_md(m.corr())
    bw = weights["B 逆波動(252日,上限40%)"]
    OUT["port_invvol_weights"] = C.df_md(bw.resample("YE").last())
    OUT["port_boot"] = boot_rows([(n, curves[n], spy, "SPY_BH") for n in list(allocs)[:3]])
    bl = ["| 配置 | 歷史 MaxDD | 區塊自助法 MaxDD 中位數 | 5% 最壞情境 | P(MaxDD < -35%) |", "|---|---|---|---|---|"]
    for n in list(allocs)[:3]:
        med, p5, prob = boot_maxdd(curves[n])
        bl.append(f"| {n} | {C.max_dd((1 + curves[n]).cumprod()) * 100:.1f}% | {med * 100:.1f}% | {p5 * 100:.1f}% | {prob * 100:.0f}% |")
    OUT["port_bootdd"] = "\n".join(bl)

    # ex-post scan (labelled as such in the report): scale TQQQ+BTC jointly
    scan = ["| desk | tqqq | mr | btc | 全期 CAGR | MaxDD | Sharpe |", "|---|---|---|---|---|---|---|"]
    for wt, wb in [(0.0, 0.0), (0.1, 0.05), (0.15, 0.10), (0.2, 0.1), (0.25, 0.15), (0.3, 0.15), (0.4, 0.2)]:
        rest = 1 - wt - wb
        w = {"desk": rest * 0.7, "tqqq": wt, "mr": rest * 0.3, "btc": wb}
        r, _ = combine(sl[cols], fixed(w, cols), cost)
        s = C.stats(r)
        scan.append(f"| {w['desk']:.2f} | {wt:.2f} | {w['mr']:.2f} | {wb:.2f} | {s['CAGR'] * 100:.1f}% | {s['MaxDD'] * 100:.1f}% | {s['Sharpe']:.2f} |")
    OUT["port_scan"] = "\n".join(scan)

    # robustness: the stock MR basket is survivorship-flattered. Swap it for the
    # survivorship-free sector-ETF version, or drop it, keeping A's other weights.
    sl_r = sl.copy()
    sl_r["mr_sect"] = mr["basket_sectors_rsi2@5bp"].reindex(nyse).fillna(0.0)
    rob = {
        "A 原版(股票 MR)": (["desk", "tqqq", "mr", "btc"], {"desk": .5, "tqqq": .2, "mr": .2, "btc": .1}),
        "A1 MR 換板塊 ETF 版": (["desk", "tqqq", "mr_sect", "btc"], {"desk": .5, "tqqq": .2, "mr_sect": .2, "btc": .1}),
        "A2 拿掉 MR(desk 70)": (["desk", "tqqq", "btc"], {"desk": .7, "tqqq": .2, "btc": .1}),
        "A3 拿掉 TQQQ(desk 70)": (["desk", "mr", "btc"], {"desk": .7, "mr": .2, "btc": .1}),
        "A4 拿掉 BTC(desk 60)": (["desk", "tqqq", "mr"], {"desk": .6, "tqqq": .2, "mr": .2}),
    }
    rl = ["| 配置 | CAGR | MaxDD | Sharpe | 自助法 MaxDD 中位數 | 5% 最壞 | P(<-35%) |", "|---|---|---|---|---|---|---|"]
    for n, (cs, w) in rob.items():
        r, _ = combine(sl_r[cs], fixed(w, cs), cost)
        s = C.stats(r)
        med, p5, prob = boot_maxdd(r)
        rl.append(f"| {n} | {s['CAGR'] * 100:.1f}% | {s['MaxDD'] * 100:.1f}% | {s['Sharpe']:.2f} | "
                  f"{med * 100:.1f}% | {p5 * 100:.1f}% | {prob * 100:.0f}% |")
    OUT["port_robust"] = "\n".join(rl)

    # 2000-2003: no desk book or BTC history. Isolate what a TQQQ-timing sleeve of
    # weight w (rest in T-bills, monthly rebalanced) alone did to the account.
    syn = lo["synth"]["TQQQsyn_sma200_b2"]
    sidx = syn.loc["1999-12-01":"2003-12-31"].index
    scash = cash.reindex(sidx).fillna(0)
    sl3 = pd.DataFrame({"tqqq": syn.reindex(sidx), "cash": scash})
    st = ["| TQQQ 權重(其餘國庫券) | 2000-2003 MaxDD | 1987-10 前後 MaxDD(1987-1988) |", "|---|---|---|"]
    syn87 = lo["synth"]["TQQQsyn_sma200_b2"].loc["1987-01-01":"1988-12-31"]
    c87 = cash.reindex(syn87.index).fillna(0)
    for w in (0.1, 0.2, 0.3):
        r, _ = combine(sl3, fixed({"tqqq": w, "cash": 1 - w}, ["tqqq", "cash"]), cost)
        r87, _ = combine(pd.DataFrame({"tqqq": syn87, "cash": c87}), fixed({"tqqq": w, "cash": 1 - w}, ["tqqq", "cash"]), cost)
        st.append(f"| {w:.0%} | {C.max_dd((1 + r).cumprod()) * 100:.1f}% | {C.max_dd((1 + r87).cumprod()) * 100:.1f}% |")
    worst = syn.loc["1987-10-01":"1987-10-31"]
    st.append("")
    st.append(f"合成 TQQQ 計時(b2)1987-10 單月報酬:{((1 + worst).prod() - 1) * 100:.1f}%;"
              f"最差單日 {worst.min() * 100:.1f}%({worst.idxmin():%Y-%m-%d})")
    OUT["tqqq_stress"] = "\n".join(st)

    # long stress 2005+: desk(long) + TQQQ (synthetic before 2010-02-11, real after) + MR basket; BTC = cash pre-2015
    dl = desk["long"]["ret"]
    idx = dl.index
    syn = lo["synth"]["TQQQsyn_sma200_b2"].reindex(idx)
    real = lo["real"]["TQQQ_sma200_b2"].reindex(idx)
    tq = real.where(idx >= pd.Timestamp("2010-02-12"), syn)
    b = crypto.to_trading_days(cr["ret"]["BTC_sma100"], idx)
    b = b.where(idx >= pd.Timestamp("2015-01-01"), cash.reindex(idx))
    sl2 = pd.DataFrame({"desk": dl, "tqqq": tq, "mr": mr["basket_stocks_rsi2@5bp"].reindex(idx), "btc": b}).fillna(0.0)
    rows2, c2 = [], {}
    spy2 = close["SPY"].dropna().pct_change().reindex(idx).fillna(0)
    for name, fn in list(allocs.items())[:3]:
        r, _ = combine(sl2[cols], fn if not name.startswith("B") else inverse_vol(cols), cost)
        c2[name] = r
        for s, e, lbl in [("2005-01-01", "2009-12-31", "2005-2009"), (None, None, "2005-今")]:
            rows2.append(row(name, r, s, e, label=lbl))
    for s, e, lbl in [("2005-01-01", "2009-12-31", "2005-2009"), (None, None, "2005-今")]:
        rows2.append(row("SPY_BH", spy2, s, e, label=lbl))
    OUT["port_long"] = C.fmt_table(rows2, BCOLS) + "\n\n" + "\n\n".join(dd_table(n + "(2005 起)", c2[n]) for n in c2)
    sl2_corr = sl2.loc[:"2014"].corr().round(2)
    OUT["port_long_corr"] = C.df_md(sl2_corr)
    KEY["port"] = {n: C.stats(curves[n]) for n in curves}


def main():
    C.RESULTS.mkdir(exist_ok=True)
    core = C.fetch(["QQQ", "TQQQ", "SOXL", "SOXX", "SMH", "^SOX", "^NDX", "SPY", "BIL", "SHV", "^IRX",
                    "^GSPC", "BTC-USD", "ETH-USD"], "1985-01-01", name="core")
    stocks_long = C.fetch(ALL_SYMBOLS, "2003-01-01", name="desk_universe_long")
    lo = section_letf(core)
    mr = section_meanrev(core, stocks_long)
    cr = section_crypto(core)
    desk = section_desk()
    section_portfolio(core, lo, mr, cr, desk)
    for k, v in OUT.items():
        (C.RESULTS / f"{k}.md").write_text(v + "\n")
    meta = {"core_fetched_at": str(core["fetched_at"].max()), "last_date": str(core["date"].max().date())}
    (C.RESULTS / "meta.json").write_text(json.dumps(meta, indent=2))
    print("\n\n".join(f"## {k}\n{v}" for k, v in OUT.items()))


if __name__ == "__main__":
    main()
