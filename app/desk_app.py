"""Trading Desk console: monitoring and control for the automated desk.

    .venv/bin/streamlit run app/desk_app.py

The system trades by itself (Alpaca paper). The human here watches, pauses,
reviews and changes rules; there is no per-trade approval.
"""
from __future__ import annotations

import json
import os

import pandas as pd
import streamlit as st

from stockmoney.desk import backtest, data, run, store
from stockmoney.desk.execution.ledger import Ledger
from stockmoney.desk.strategies import REGISTRY

st.set_page_config(page_title="Trading Desk", layout="wide")


@st.cache_data(ttl=600)
def _bars():
    return data.to_bars(data.load())


def _q(sql: str, params=()) -> pd.DataFrame:
    with store.connect() as con:
        run.ensure_schema(con)
        Ledger(con)
        return pd.read_sql(sql, con, params=params)


with store.connect() as con:
    run.ensure_schema(con)
    halt = store.get_setting(con, "halt_new_entries", "0") == "1"
halt_file = os.path.exists("data/desk/HALT")

st.sidebar.title("Trading Desk")
st.sidebar.caption(f"券商:Alpaca({os.environ.get('ALPACA_ENV', 'paper')})")
if halt or halt_file:
    st.sidebar.error("新進場已暫停" + ("(HALT 檔案)" if halt_file else ""))
else:
    st.sidebar.success("自動交易中")
page = st.sidebar.radio("頁面", ["總覽", "持倉與委託", "Muse", "覆盤日誌", "策略實驗室"])

# ===================================================================== overview
if page == "總覽":
    eq = _q("SELECT * FROM equity_history ORDER BY date")
    c1, c2, c3 = st.columns(3)
    if not eq.empty:
        last = eq.iloc[-1]
        c1.metric("系統淨值", f"{last.system_equity:,.0f}", f"{last.system_equity / 50_000 - 1:+.2%}")
        peak = eq.system_equity.cummax().iloc[-1]
        c2.metric("距高點回撤", f"{last.system_equity / peak - 1:.2%}")
        c3.metric("狀態", last["mode"])
        bars = _bars()
        df = eq.set_index(pd.to_datetime(eq.date))[["system_equity"]] / 50_000 - 1
        spy = bars.close["SPY"].reindex(df.index).ffill()
        df["SPY"] = spy / spy.iloc[0] - 1
        st.line_chart(df * 100, y_label="累計報酬 %")
    else:
        st.info("尚無淨值紀錄。第一次盤後任務執行後會出現。")

    st.subheader("總開關")
    new_halt = st.toggle("暫停所有新進場(出場與停損照常運作)", value=halt)
    if new_halt != halt:
        with store.connect() as con:
            store.set_setting(con, "halt_new_entries", "1" if new_halt else "0")
        st.rerun()

    j = _q("SELECT date, auto_summary FROM journal ORDER BY date DESC LIMIT 1")
    if not j.empty:
        st.divider()
        st.markdown(j.auto_summary.iloc[0] or "")

# ===================================================================== positions & orders
elif page == "持倉與委託":
    st.subheader("持倉(依策略歸屬)")
    pos = _q("SELECT p.symbol, p.strategy, p.qty, p.avg_price, p.stop, m.initial_stop, m.highest_close, m.entry_date "
             "FROM sleeve_positions p LEFT JOIN position_meta m USING(symbol)")
    if pos.empty:
        st.caption("空手")
    else:
        close = _bars().close.iloc[-1]
        pos["last"] = pos.symbol.map(lambda s: float(close.get(s, float("nan"))))
        pos["損益%"] = (pos["last"] / pos.avg_price - 1) * 100
        pos["距停損%"] = (pos["last"] / pos.stop - 1) * 100
        st.dataframe(pos.round(2), hide_index=True, use_container_width=True)
    st.subheader("明日計畫")
    st.dataframe(_q("SELECT plan_date, symbol, side, qty, strategy, stop, status, note, reason FROM planned_orders "
                    "ORDER BY created_at DESC LIMIT 50"), hide_index=True, use_container_width=True)
    st.subheader("已送出委託")
    st.dataframe(_q("SELECT submitted_at, kind, symbol, side, qty, stop, status, strategy FROM submitted "
                    "ORDER BY submitted_at DESC LIMIT 100"), hide_index=True, use_container_width=True)
    st.subheader("成交")
    fills = _q("SELECT filled_at, symbol, strategy, side, qty, price, realized_pnl FROM fills ORDER BY id DESC")
    st.dataframe(fills, hide_index=True, use_container_width=True)
    if not fills.empty:
        st.subheader("各策略已實現損益")
        st.dataframe(fills.groupby("strategy").realized_pnl.agg(["count", "sum"]), use_container_width=True)

# ===================================================================== muse
elif page == "Muse":
    st.caption("Muse 的訊息只能降低風險(擋進場、禁止加碼、通知),不能開倉或改參數。文字內容僅供閱讀。")
    try:
        from stockmoney.desk.bridge.inbox import ensure_schema as bridge_schema
        with store.connect() as con:
            bridge_schema(con)
        from datetime import datetime, timezone
        from stockmoney.desk import muse_ops
        with store.connect() as con:
            card = muse_ops.scorecard(con, datetime.now(timezone.utc))
            stage = store.get_setting(con, "muse_stage", "0")
        st.subheader(f"Muse 成績單(近 7 天,上線階段 {stage})")
        c = st.columns(4)
        for i, (k, v) in enumerate(card["deliveries"].items()):
            c[i % 3].metric(v["name"], f"{v['on_time']}/{v['due']}", help="準時送達 / 應送達")
        c[3].metric("查核回覆", f"{card['research']['answered']}/{card['research']['requested']}")
        st.caption(f"通過驗證 {card['accepted']} 封,被拒 {card['rejected']} 封。上線步驟見 docs/desk/MUSE_ONBOARDING.md")
        st.subheader("收到的訊息")
        st.dataframe(_q("SELECT received_at, type, msg_id, parsed_json FROM bridge_messages ORDER BY received_at DESC LIMIT 100"),
                     hide_index=True, use_container_width=True)
        st.subheader("被拒絕的訊息")
        st.dataframe(_q("SELECT * FROM bridge_rejects ORDER BY rowid DESC LIMIT 50"), hide_index=True, use_container_width=True)
    except Exception as e:
        st.info(f"Muse 橋樑尚未啟用:{e}")

# ===================================================================== journal
elif page == "覆盤日誌":
    days = _q("SELECT date FROM journal ORDER BY date DESC").date.tolist()
    if not days:
        st.info("尚無日誌")
        st.stop()
    day = st.selectbox("日期", days)
    with store.connect() as con:
        j = dict(con.execute("SELECT * FROM journal WHERE date=?", (day,)).fetchone())
    st.markdown(j["auto_summary"] or "")
    with st.form("journal"):
        mv = st.text_area("我對市場的判斷(之後拿來檢驗直覺準不準)", j.get("market_view") or "")
        ls = st.text_area("今天學到什麼 / 新策略假設", j.get("lessons") or "")
        ms = st.text_area("系統或我犯的錯", j.get("mistakes") or "")
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
            out = {"目前": backtest.run_single(bars, cls(**cur)), "新參數": backtest.run_single(bars, cls(**edited))}
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
            st.success("已套用,下次盤後任務生效")
    st.subheader("參數變更紀錄")
    st.dataframe(changes, hide_index=True, use_container_width=True)
