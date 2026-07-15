# Worker-3 自主執行規格(排程 2026-07-14 02:32 觸發,無人看管)

> 這份檔案是給「半夜自動觸發的全新 session」看的。它不記得先前對話 —— 所有需要的東西都在這裡 + MEMORY.md + REBUILD_PLAN.md。
> **鐵則:自主執行,絕不停下來問使用者問題(使用者在睡覺)。任何岔路都照下方「預設分支」走,把過程與結論寫進晨間報告。不 commit。不擴張範圍。**

## 你是誰、要做什麼
你是這個 stockmoney 量化專案的執行者,做「實驗三:真實新聞訊號有沒有加值」。背景見 MEMORY.md(phase0-timemachine-verdict、rebuild-plan)與 REBUILD_PLAN.md §4.11–4.12。一句話:四個實驗證明純價格訊號打不贏「單純做多」(`long_stock`);唯一未測槓桿=真實新聞。Worker-1 已回填**真實** GDELT GKG per-symbol tone+volume(pilot 2018-19 在 `data/news_backfill/`,腳本 `scripts/backfill_news_real.py`);Worker-2 已建記分板 `src/stockmoney/backtest/scoreboard.py`(有 `Strategy` protocol 可即插即用)。

## 環境
- repo 根:`/Users/danielkang/Documents/stockmoney-main`;一律用 `.venv/bin/python`(uv 不在 PATH)。
- DB 唯讀:`STOCKMONEY_DB=/Users/danielkang/Documents/stockmoney-main/data/stockmoney_live.duckdb`。
- model:`claude-sonnet-5`;effort 意圖:**high**(leak-safety + 記分板整合,結論要可信)。工作要仔細、給證據。

## 任務步驟
1. **補滿新聞資料(有硬閘門)**:用 `scripts/backfill_news_real.py` 把 9 檔補到 2018–2026。**先跑 BigQuery dry-run 估算**。
2. **新聞特徵(leak-safe)**:每日 per-symbol tone 均值、文章量、短窗 z-score/加速度,**只用 `available_ts < 決策時刻` 的新聞**。放 `src/stockmoney/backtest/news_features.py`(新檔)。
3. **記分板策略 plug-in(不改 scoreboard.py 核心)**,放 `src/stockmoney/backtest/news_strategies.py`:
   - `sellput_otm5_newsgated`:負面 tone/爆量時空手(風險閘門),baseline = `sellput_otm5_naive`。
   - `news_catalyst_dir`:強 tone+爆量時做方向,baseline = `B2_random` 與 `long_stock`。
4. **跑記分板**,回答兩問:①新聞閘門能否砍尾端又「不」像價格閘門那樣殺死報酬?②新聞催化能否打贏隨機/做多?
5. **寫 `NEWS_EXPERIMENT_REPORT.md`**(晨間交付):結論 + 數字表 + 但書。

## 預設分支(遇到就照這走,不要問)
- **dry-run 估算 > BigQuery 免費額度(1TB/月)剩餘**:**不要**跑 8 年全量。改用現有 2018-19 pilot 出一版初步結果,在報告明講「因免費額度限制只用 2 年,結論為初步、統計力弱」。
- **BigQuery 認證/網路失敗**:同上,用 pilot;報告記錄錯誤。
- **新聞訊號測不出加值(null)**:**照實報 null**,並註明關鍵但書 —— GKG per-symbol 是「有提到」非「主要在講」(噪音),null 可能是資料噪音而非「新聞無用」;建議下一步去拿真標題(DOC 2.0 API,先前在沙盒被 429 擋掉,值得換網路重試)。null 誠實回報 = 成功,不是失敗。
- **新聞閘門有效(砍尾端又保報酬)**:記錄為正面發現,在報告提出「下一步把它接進 production + 建 discovery/劇本」的建議,但**不要真的動手建**(範圍只到實驗)。
- **測試/import 失敗**:自己 debug 修好;修不動就把已完成部分寫進報告,不要卡死。

## 收尾規則
- **不 commit**(使用者要求),留在工作區。
- 產出:`news_features.py`、`news_strategies.py`、`NEWS_EXPERIMENT_REPORT.md`、(可能)擴充的 `data/news_backfill/`。
- 報告開頭放一段「給使用者的三行晨間摘要」:做了什麼、結論、建議下一步。
- 全程給證據(真實指令輸出、真實數字),不宣稱、不誇大綠燈。
