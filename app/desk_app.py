"""Trading Desk console: the only place a human acts on the system.

    .venv/bin/streamlit run app/desk_app.py

Everything here is paper trading. No broker connection exists in this code.
"""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from stockmoney.desk import backtest, data, live, store
from stockmoney.desk.strategies import REGISTRY

st.set_page_config(page_title="Trading Desk", layout="wide")


@st.cache_data(ttl=600)
def _bars():
    return data.to_bars(data.load())


def _accounts():
    with store.connect() as con:
        return store.load_accounts(con)


def _signals(day: str) -> dict[str, dict]:
    with store.connect() as con:
        rows = con.execute("SELECT * FROM signals WHERE date=?", (day,)).fetchall()
    return {r["signal_id"]: dict(r) for r in rows}


def _layers_md(layers: list[dict]) -> str:
    return "\n".join(f"- [{'v' if l['ok'] else 'x'}] **{l['layer']}**:{l['note']}" for l in layers)


accts = _accounts()
if not accts:
    st.warning("尚未初始化。先執行 `.venv/bin/python scripts/desk_daily.py`。")
    st.stop()

with store.connect() as con:
    paper_start = store.get_setting(con, "paper_start", "?")
    halt = store.get_setting(con, "halt_new_entries", "0") == "1"
    last_run = con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()

st.sidebar.title("Trading Desk")
st.sidebar.caption(f"紙上交易,起始 {paper_start}")
if last_run:
    st.sidebar.caption(f"最後執行 {last_run['run_at']}(資料至 {last_run['through_date']})")
if halt:
    st.sidebar.error("已暫停新進場")
page = st.sidebar.radio("頁面", ["今日決策", "持倉", "績效", "覆盤日誌", "策略實驗室"])

desk, _, desk_last = accts["desk"]

# ===================================================================== today
if page == "今日決策":
    with store.connect() as con:
        j = con.execute("SELECT auto_summary FROM journal ORDER BY date DESC LIMIT 1").fetchone()
    if j:
        st.markdown(j["auto_summary"])
    st.divider()
    pending = [o for o in desk.orders if o.status == "pending_approval"]
    st.subheader(f"待審核進場 {len(pending)} 筆")
    st.caption("核准的單會在下一個開盤以紙上價格成交;未處理的單在下一次執行時自動作廢。拒絕或調整倍數請寫理由,這些紀錄每季拿來檢查「人的判斷有沒有加分」。")
    sigs = _signals(desk_last)
    for o in pending:
        s = sigs.get(o.signal_id, {})
        layers = json.loads(s.get("layers_json") or "[]")
        warn = any(not l["ok"] for l in layers)
        with st.expander(f"{o.symbol} | {REGISTRY[o.strategy].label} | {o.shares} 股 @~{s.get('ref_price', 0):.2f} | 停損 {o.stop:.2f}" + ("  [注意]" if warn else ""), expanded=True):
            st.markdown(f"**論點**:{s.get('thesis', o.reason)}")
            st.markdown(f"**失效條件**:{s.get('invalidation', '')}")
            st.markdown(_layers_md(layers))
            risk = (s.get("ref_price", 0) - o.stop) * o.shares
            st.caption(f"部位 {o.shares * s.get('ref_price', 0):,.0f} USD,停損風險約 {risk:,.0f} USD")
            c1, c2, c3 = st.columns([1, 2, 1])
            mult = c1.select_slider("部位倍數", [0.5, 0.75, 1.0, 1.25, 1.5], value=1.0, key=f"m{o.order_id}")
            note = c2.text_input("理由(拒絕或調倍數必填)", key=f"n{o.order_id}")
            if c3.button("核准", key=f"a{o.order_id}", type="primary"):
                if mult != 1.0 and not note:
                    st.error("調整倍數請寫理由")
                else:
                    live.decide(o.order_id, True, mult, note)
                    st.rerun()
            if c3.button("拒絕", key=f"r{o.order_id}"):
                if not note:
                    st.error("拒絕請寫理由")
                else:
                    live.decide(o.order_id, False, 0.0, note)
                    st.rerun()

# ===================================================================== positions
elif page == "持倉":
    bars = _bars()
    close = bars.close.iloc[-1]
    for name, (acct, kind, _) in accts.items():
        st.subheader(f"{name}{'(人工審核)' if kind == 'human' else '(全自動)'}")
        if not acct.positions:
            st.caption("空手")
            continue
        rows = []
        for p in acct.positions.values():
            c = float(close.get(p.symbol, p.entry_price))
            rows.append({
                "標的": p.symbol, "策略": REGISTRY[p.strategy].label, "股數": p.shares,
                "進場日": p.entry_date, "成本": round(p.entry_price, 2), "現價": round(c, 2),
                "損益%": round((c / p.entry_price - 1) * 100, 2), "移動停損": round(p.stop, 2),
                "距停損%": round((c / p.stop - 1) * 100, 1),
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        if kind == "human":
            with st.form("manual_exit"):
                sym = st.selectbox("人工出場(下個開盤成交)", list(acct.positions))
                note = st.text_input("理由(必填,例如:消息面改變、論點失效)")
                if st.form_submit_button("送出出場") and note:
                    live.manual_exit(sym, note)
                    st.rerun()

# ===================================================================== performance
elif page == "績效":
    bars = _bars()
    curves = {}
    for name, (acct, _, _) in accts.items():
        if acct.equity_curve:
            e = pd.DataFrame(acct.equity_curve)
            curves[name] = e.set_index(pd.to_datetime(e["date"]))["equity"] / acct.starting_cash - 1
    if curves:
        df = pd.DataFrame(curves)
        spy = bars.close["SPY"].reindex(df.index)
        df["SPY"] = spy / spy.iloc[0] - 1  # same start as the accounts' first marked close
        st.line_chart(df * 100, y_label="累計報酬 %")
    rows = []
    for name, (acct, _, _) in accts.items():
        t = pd.DataFrame(acct.trades)
        stats = backtest.trade_stats(t) if not t.empty else {"n": 0}
        rows.append({"帳戶": name, **stats})
    st.dataframe(pd.DataFrame(rows), hide_index=True)
    st.caption("看期望值(avg_ret、PF)而不是只看勝率:順勢策略常見勝率 35-45%,靠大賺小賠獲利。一個月樣本很小,信賴區間會很寬。")
    with store.connect() as con:
        dec = pd.read_sql("SELECT * FROM decisions ORDER BY id DESC", con)
    st.subheader("人工決策紀錄")
    st.dataframe(dec, hide_index=True, use_container_width=True)

# ===================================================================== journal
elif page == "覆盤日誌":
    with store.connect() as con:
        days = [r["date"] for r in con.execute("SELECT date FROM journal ORDER BY date DESC").fetchall()]
    if not days:
        st.info("尚無日誌")
        st.stop()
    day = st.selectbox("日期", days)
    with store.connect() as con:
        j = dict(con.execute("SELECT * FROM journal WHERE date=?", (day,)).fetchone())
    st.markdown(j["auto_summary"] or "")
    closed = [tr for a, _, _ in accts.values() for tr in a.trades if tr["exit_date"] == day]
    if closed:
        st.dataframe(pd.DataFrame(closed), hide_index=True)
    with st.form("journal"):
        mv = st.text_area("我對市場的判斷(機器看不到的:政策、情緒、消息傳導)", j.get("market_view") or "")
        ls = st.text_area("今天學到什麼 / 新特徵假設", j.get("lessons") or "")
        ms = st.text_area("犯的錯(衝動、沒照規則、猶豫)", j.get("mistakes") or "")
        tg = st.text_input("標籤(逗號分隔)", j.get("tags") or "")
        if st.form_submit_button("儲存"):
            with store.connect() as con:
                con.execute("UPDATE journal SET market_view=?, lessons=?, mistakes=?, tags=?, updated_at=? WHERE date=?",
                            (mv, ls, ms, tg, store.now(), day))
            st.success("已儲存")

# ===================================================================== lab
elif page == "策略實驗室":
    st.caption("改參數的正確流程:提出假設 -> 在這裡回測驗證 -> 寫理由後才套用。套用只影響之後的訊號,不改寫歷史。")
    with store.connect() as con:
        params = store.strategy_params(con)
        disabled = set(json.loads(store.get_setting(con, "disabled_strategies", "[]")))
        changes = pd.read_sql("SELECT * FROM param_changes ORDER BY id DESC", con)
    new_halt = st.toggle("暫停人工帳戶所有新進場(總開關)", value=halt)
    if new_halt != halt:
        with store.connect() as con:
            store.set_setting(con, "halt_new_entries", "1" if new_halt else "0")
        st.rerun()
    name = st.selectbox("策略", list(REGISTRY), format_func=lambda n: f"{REGISTRY[n].label} ({n})")
    cls = REGISTRY[name]
    st.write(cls.description)
    cur = {**cls.params, **params.get(name, {})}
    edited = {k: st.number_input(k, value=float(v), key=f"p{name}{k}") for k, v in cur.items()}
    enabled = st.checkbox("啟用", value=name not in disabled)
    c1, c2 = st.columns(2)
    if c1.button("用新參數回測(2015 至今)"):
        with st.spinner("回測中(約 1 分鐘)..."):
            bars = _bars()
            out = {
                "目前": backtest.run_single(bars, cls(**cur)),
                "新參數": backtest.run_single(bars, cls(**edited)),
            }
            st.dataframe(pd.DataFrame(out).T)
            st.caption("注意:在同一段歷史上反覆調參數會過擬合。只採用有事前理由、且改善幅度明顯的變更。")
    reason = c2.text_input("套用理由(必填)")
    if c2.button("套用", type="primary"):
        if not reason:
            st.error("請寫理由")
        else:
            with store.connect() as con:
                for k, v in edited.items():
                    if v != float(cur[k]):
                        store.change_param(con, name, k, v, reason)
                d = disabled - {name} if enabled else disabled | {name}
                store.set_setting(con, "disabled_strategies", json.dumps(sorted(d)))
            st.success("已套用,下次每日執行生效")
    st.subheader("參數變更紀錄")
    st.dataframe(changes, hide_index=True, use_container_width=True)
