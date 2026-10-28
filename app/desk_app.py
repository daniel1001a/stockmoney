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

from stockmoney.desk import backtest, console, data, run, store
from stockmoney.desk.execution.ledger import Ledger
from stockmoney.desk.strategies import REGISTRY

st.set_page_config(page_title="Trading Desk", layout="wide")


NO_PRICES = "找不到價格快取(data/desk/ohlcv.parquet)。先跑一次 scripts/desk_daily.py 下載價格,這一頁才有圖與最新價。"


@st.cache_data(ttl=600)
def _bars():
    return data.to_bars(data.load())


def _bars_or_none():
    """Bars, or None when the price cache has not been downloaded yet."""
    if not (data.DEFAULT_DIR / "ohlcv.parquet").exists():
        return None
    try:
        return _bars()
    except FileNotFoundError:
        return None


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
page = st.sidebar.radio("頁面", ["總覽", "持倉與委託", "情報員", "學習", "覆盤日誌", "策略實驗室"])

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
        bars = _bars_or_none()
        df = eq.set_index(pd.to_datetime(eq.date))[["system_equity"]] / 50_000 - 1
        if bars is None:
            st.warning(NO_PRICES)
        else:
            spy = bars.close["SPY"].reindex(df.index).ffill()
            df["SPY"] = spy / spy.iloc[0] - 1
        st.line_chart(df * 100, y_label="累計報酬 %")
    else:
        st.info("尚無淨值紀錄。第一次盤後任務執行後會出現。")

    st.subheader("總開關")
    st.caption("暫停所有新進場(出場與停損照常運作)。解除暫停必須寫理由,會記入參數變更紀錄。")
    halt_reason = st.text_input("理由(解除暫停時必填)", key="halt_reason")
    if halt:
        if st.button("解除暫停新進場", type="primary"):
            try:
                with store.connect() as con:
                    console.set_halt(con, False, halt_reason)
            except ValueError as e:
                st.error(str(e))
            else:
                st.rerun()
    elif st.button("暫停所有新進場"):
        with store.connect() as con:
            console.set_halt(con, True, halt_reason)
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
        bars = _bars_or_none()
        if bars is None:
            st.warning(NO_PRICES)
        else:
            close = bars.close.iloc[-1]
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

# ===================================================================== agents
elif page == "情報員":
    from datetime import datetime, timezone

    from stockmoney.desk import muse_ops
    from stockmoney.desk.bridge.inbox import ensure_schema as bridge_schema
    from stockmoney.desk.learn import views

    st.caption("兩位情報員是不同的 agent,資料分開記錄。它們的訊息只能降低風險(擋進場、禁止加碼、通知),"
               "不能開倉或改參數。Pika(Grok)v1 只儲存,不影響任何交易。文字內容僅供閱讀。")
    try:
        with store.connect() as con:
            bridge_schema(con)
            card = muse_ops.scorecard(con, datetime.now(timezone.utc))
            stage = store.get_setting(con, "muse_stage", "0")
            grok_on = bool(json.loads(store.get_setting(con, muse_ops.GROK_EXPECT_KEY, "[]")))
            split = views.scorecard_by_agent(card)
            last_msg = {a: con.execute("SELECT MAX(received_at) FROM bridge_messages WHERE type" +
                                       ("='GROK_DAILY'" if a == views.GROK else "!='GROK_DAILY'")).fetchone()[0]
                        for a in (views.MUSE, views.GROK)}
            tabs = st.tabs(["總覽", views.MUSE, views.GROK])
            with tabs[0]:
                col_m, col_g = st.columns(2)
                with col_m.container(border=True):
                    st.subheader(views.MUSE)
                    st.caption(f"Threads、Instagram、Reddit 等的社群快照、喊單、警示、查核回覆。上線階段 {stage}")
                    if stage == "0":
                        st.caption("上線階段 0:漏交檢查尚未啟用,下面的 0 不代表漏交,只是還沒開始計。")
                    for k, v in split[views.MUSE].items():
                        st.metric(v["name"], f"{v['on_time']}/{v['due']}", help="近 7 天準時送達 / 應送達")
                    st.metric("查核回覆", f"{card['research']['answered']}/{card['research']['requested']}")
                    st.caption(f"最近一封:{last_msg[views.MUSE] or '尚無'}")
                with col_g.container(border=True):
                    st.subheader(views.GROK)
                    st.caption("X 上的事件、事件鏈、喊單、熱度。" + ("漏交檢查已開啟" if grok_on else "漏交檢查尚未開啟"))
                    for k, v in split[views.GROK].items():
                        label = v["name"] if grok_on else f"{v['name']}(檢查未啟用)"
                        st.metric(label, f"{v['on_time']}/{v['due']}", help="近 7 天準時送達 / 應送達(只算交易日早報)")
                    today = muse_ops.grok_today(con, datetime.now(timezone.utc))
                    shown = st.error if today["status"] == "missed" else st.caption
                    shown("今天早報:" + muse_ops.grok_today_text(today))
                    n_chain = con.execute("SELECT COUNT(*) FROM chains WHERE source='grok'").fetchone()[0]
                    st.metric("已進入評分的事件鏈", n_chain)
                    st.caption(f"最近一封:{last_msg[views.GROK] or '尚無'}")
                st.caption(f"通過驗證 {card['accepted']} 封,被拒 {card['rejected']} 封(近 7 天,兩者合計)。"
                           "上線步驟見 docs/desk/MUSE_ONBOARDING.md、docs/desk/GROK_PROMPT.md")
            with tabs[1]:
                st.subheader("收到的訊息")
                st.dataframe(views.messages(con, views.MUSE), hide_index=True, alt="Muse 收到的訊息")
                st.subheader("被拒絕的訊息")
                st.dataframe(views.rejects(con, views.MUSE), hide_index=True, alt="Muse 被拒絕的訊息")
            with tabs[2]:
                g = views.latest_grok(con)
                if g is None:
                    st.info("尚未收到 GROK_DAILY。第一封預計由 Pika 在 20:00 ET(晚報)或 07:30 ET(早報)寄出。")
                else:
                    d = g["data"]
                    st.caption(f"最新一封:{g['msg_id']},我們收到的時間 {g['received_at']}"
                               "(這可能是前一天的信;今天是否漏交見總覽)")
                    if not d["events"]:
                        st.info("這封是空白日報:Pika 有寄信,回報沒有事件。")
                    st.subheader("事件")
                    st.dataframe(pd.DataFrame(d["events"]), hide_index=True, alt="Grok 事件")
                    st.subheader("事件鏈")
                    for c in d["chains"]:
                        with st.container(border=True):
                            st.markdown(f"**{c['trigger']}**(信心 {c['confidence']:.0%})")
                            st.write(" -> ".join(c["steps"]))
                            st.caption("影響:" + "、".join(f"{i['ticker']} {i['direction']} {i['horizon_days']} 天"
                                                         for i in c["impacts"]))
                    st.subheader("喊單")
                    st.dataframe(pd.DataFrame(d["calls"]), hide_index=True, alt="Grok 記錄的喊單")
                    st.subheader("熱度變化")
                    st.caption("熱度只供閱讀,不進任何計算或門檻;沒有提供時這裡是空的。")
                    st.dataframe(pd.DataFrame(d.get("attention") or []), hide_index=True, alt="Grok 熱度變化")
                    if d.get("market_risk"):
                        mr = d["market_risk"]
                        st.subheader("大盤風險判讀(只記錄,不影響下單)")
                        st.write(f"{mr['level']}:{mr['summary']}")
                st.subheader("收到的訊息")
                st.dataframe(views.messages(con, views.GROK), hide_index=True, alt="Pika 收到的訊息")
                st.subheader("被拒絕的訊息")
                st.dataframe(views.rejects(con, views.GROK), hide_index=True, alt="Pika 被拒絕的訊息")
    except Exception as e:
        st.info(f"橋樑尚未啟用:{e}")

# ===================================================================== learning
elif page == "學習":
    from datetime import date

    from stockmoney.desk.learn import chains as chain_mod
    from stockmoney.desk.learn import earnings, tables, views
    from stockmoney.desk.universe import STOCKS

    st.caption("學習層只讀取帳本與價格,不會下單。每個交易日 17:15 ET 更新一次。"
               "樣本很少時,所有數字只當流程驗證,不要當結論。")
    with store.connect() as con:
        tables.ensure_schema(con)
        tabs = st.tabs(["影子帳戶", "驗屍卡", "事件鏈", "財報日"])
        with tabs[0]:
            shown, summary = views.shadow_table(con)
            if shown.empty:
                st.info("影子帳戶尚無資料。從 2026-10-12 起每個交易日 17:15 ET 累積。")
            else:
                st.line_chart(shown, y_label="累計報酬 %")
                st.dataframe(summary, alt="影子帳戶累計報酬與最大回撤")
                st.caption("對照組:隨機進場用同一份清單、同樣的停損與大盤濾網。現行規則要穩定贏過它,才算進場訊號有價值。")
        with tabs[1]:
            pm = views.postmortems(con)
            if pm.empty:
                st.info("尚無平倉交易,沒有驗屍卡。")
            else:
                st.dataframe(pm.drop(columns=["lesson"]).assign(ret=lambda d: (d.ret * 100).round(2)),
                             hide_index=True, alt="驗屍卡")
                st.caption("S1 跳空穿停損、S2 被洗出場、S3 追高、S4 大盤翻轉、S5 板塊退潮、S6 事件或財報跳空、"
                           "S7 獲利回吐、S8 執行異常、S9 消息面、S0 正常虧損。規則見 learn/postmortem.py。")
                with st.form("lesson"):
                    tid = st.selectbox("交易", pm.trade_id.tolist(),
                                       format_func=lambda t: f"{pm.set_index('trade_id').loc[t, 'exit_day']} "
                                                             f"{pm.set_index('trade_id').loc[t, 'symbol']}")
                    text = st.text_input("學到什麼(一句話)")
                    if st.form_submit_button("儲存") and text.strip():
                        con.execute("UPDATE trade_postmortem SET lesson=? WHERE trade_id=?", (text.strip(), tid))
                        con.commit()
                        st.success("已儲存")
        with tabs[2]:
            level, why = chain_mod.authority(con)
            sm = chain_mod.summary(con)
            c1, c2, c3 = st.columns(3)
            c1.metric("事件鏈", sm["chains"])
            c2.metric("已評分", sm["scored"])
            c3.metric("命中率", "-" if sm["hit_rate"] is None else f"{sm['hit_rate']:.0%}")
            st.caption(f"權限階段:{level}。{why}")
            ct = views.chain_table(con)
            if not ct.empty:
                st.dataframe(ct, hide_index=True, alt="事件鏈與評分")
        with tabs[3]:
            held = [r[0] for r in con.execute("SELECT symbol FROM sleeve_positions").fetchall()] \
                if con.execute("SELECT 1 FROM sqlite_master WHERE name='sleeve_positions'").fetchone() else []
            up = earnings.upcoming(con, list(STOCKS), date.today(), 30)
            if not up:
                st.info("尚無財報日資料(學習任務第一次執行後出現)。")
            else:
                held_n = {earnings.norm(h) for h in held}
                df = pd.DataFrame([{"symbol": s, "財報日": d, "持倉": s in held_n} for s, d in sorted(up.items(), key=lambda x: x[1])])
                st.dataframe(df, hide_index=True, alt="未來 30 天的財報日")
                st.caption("來源:Nasdaq 免費清單。抓不到的日子不會被當成沒有財報。")

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
    stored = params.get(name, {})
    if set(stored) - set(cls.params):
        st.warning(f"已儲存的參數含未知鍵 {sorted(set(stored) - set(cls.params))},每日任務建立策略時會報錯;請移除。")
    cur = {k: stored.get(k, v) for k, v in cls.params.items()}
    edited = {k: st.number_input(k, value=float(v), key=f"p{name}{k}", help=f"合法範圍 {cls.bounds[k].describe()}")
              for k, v in cur.items()}
    enabled = st.checkbox("啟用", value=name not in disabled)
    c1, c2 = st.columns(2)
    if c1.button("用新參數回測(2015 至今)"):
        try:
            new_strategy = cls(**edited)
            old_strategy = cls(**cur)
        except ValueError as e:
            st.error(f"參數不合法:{e}")
        else:
            bars = _bars_or_none()
            if bars is None:
                st.error(NO_PRICES)
            else:
                with st.spinner("回測中(約 1 分鐘)..."):
                    out = {"目前": backtest.run_single(bars, old_strategy), "新參數": backtest.run_single(bars, new_strategy)}
                    st.dataframe(pd.DataFrame(out).T)
                    st.caption("注意:在同一段歷史上反覆調參數會過擬合。只採用有事前理由、且改善幅度明顯的變更。")
    reason = c2.text_input("套用理由(必填)")
    if c2.button("套用", type="primary"):
        try:
            with store.connect() as con:
                applied = console.apply_strategy_changes(con, name, edited, enabled, reason)
        except ValueError as e:
            st.error(str(e))
        else:
            st.success("已套用,下次盤後任務生效:" + ";".join(applied) if applied else "沒有任何變更")
    st.subheader("參數變更紀錄")
    st.dataframe(changes, hide_index=True, use_container_width=True)
