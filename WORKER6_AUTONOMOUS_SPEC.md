# Worker-6 自主規格 — 誠實駕駛艙 v2(排程 2026-07-14 14:00 自動觸發,無人看管)

> 給「14:00 自動觸發的全新 session」看。它不記得先前對話 —— 所需一切在此 + MEMORY.md（尤其 [[dashboard-cockpit-2026-07]]、[[phase0-timemachine-verdict-2026-07]]）+ REBUILD_PLAN.md。
> **鐵則:自主執行,絕不停下來問使用者（他不在看）。任何岔路照下方預設走。不 commit。不擴張範圍。最後留 DASHBOARD_V2_REPORT.md（頂部 3 行摘要）。**

## 環境
- repo `/Users/danielkang/Documents/stockmoney-main`；一律 `.venv/bin/python`（uv 不在 PATH）。前端 React+Vite（frontend/），後端 FastAPI（src/stockmoney/api/），dev server 用 .claude/launch.json（fastapi-backend :8000、frontend-vite :5173）。DB 唯讀 data/stockmoney_live.duckdb。
- model 意圖 Sonnet；effort 意圖 medium-high（前端迭代 + 後端訊號要正確）。

## 背景（先讀）
產品已轉向「誠實 Lin 決策支援駕駛艙」（六實驗證明無系統性方向 edge）。v1 已上線（`src/stockmoney/api/cockpit.py` + `/api/briefing` `/api/cockpit`，`frontend/src/pages/Opportunities.tsx`）。**絕不呈現方向信心/預測漲跌。** v2 = 在 v1 之上加深，全部保持誠實描述性、不預測。

## 任務（v2 = 五項加深，全部誠實、不預測）
1. **更完整的敘事總結（比 Lin 更完整、接真實世界事件）**：把每日盤前摘要從「每檔一行」升級成一段真正的敘事 —— 綜合 macro/新聞事件（Fed、油價、地緣、財報）+ regime + 板塊強弱，寫成像晨報的一段話。用既有 news（news_items / news_synthesis / catalyst）當來源。描述事實與價位，不預測漲跌。
2. **新聞相關性過濾**：去掉「AAPL Stock Quote - CNN」這種通用/無資訊標題（用簡單規則：過濾標題含 "Stock Quote/Price and Forecast/Forecast" 等模板詞、或無 catalyst 分數的），優先顯示有實質內容的。
3. **交易量訊號**：每檔加「今日量 vs 20 日均量」的相對值（放量/縮量），標在決策卡。純描述。
4. **板塊連動 / 離散度**：每檔標「今天跟不跟板塊」（用同板塊報酬離散度，呼應 CLAUDE.md 第 8 節 idiosyncratic/systematic 拆解）—— 脫離板塊獨走 = 有個股消息。
5. **資金輪轉（sector rotation）**：大盤面板加「今日各板塊強弱排行」（各 sector 當日/近 5 日平均報酬排序），看風口在哪。

每項在 UI 上要有一句白話說明（使用者是外行），例如 hover/小字：「放量突破比較可信」。

## 預設分支（遇到照走，不要問）
- 某資料源/欄位缺（如 news_synthesis 空、某板塊資料不足）→ 該項優雅降級（顯示「資料不足」而非崩潰），在報告註明。
- dev server 起不來或有 console error → 自己 debug 修好；修不動就把已完成部分寫進報告，別卡死。
- 敘事合成若沒有夠好的新聞事件 → 退回 regime + 板塊強弱的簡版敘事，註明。

## 收尾
- **不 commit**。驗證:跑 dev server、載首頁、確認零 console error、截圖/read_page 描述結果。
- 寫 `DASHBOARD_V2_REPORT.md`：頂部 3 行摘要（做了什麼/現況/建議下一步）+ 各項是否完成 + 截圖描述 + 但書。
- 檔案所有權:`src/stockmoney/api/*`、`frontend/src/*`、`DASHBOARD_V2_REPORT.md`。不動 backtest 套件與實驗腳本。
- 給證據不宣稱;誠實框架貫穿(不預測方向)。
