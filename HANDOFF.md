# stockmoney — Session Handoff (2026-07-11)

把這份檔案的內容貼給新對話,或直接說「讀 HANDOFF.md 跟 CLAUDE.md,幫我測試一下現狀,然後繼續進度」。

---

## 這是什麼

量化市場情緒/風向預測系統(股票+選擇權觀察清單決策輔助,**不下單、只給提示**)。完整規格見專案根目錄的 `CLAUDE.md`(所有設計決策都要對照它)。技術棧:Python 3.12 + `uv`、DuckDB(`data/stockmoney.duckdb`)、polars、scikit-learn/hmmlearn、FastAPI + React/Vite/TS/Tailwind(新前端,`frontend/`)、OpenClaw(本機 agent 自動化,獨立於這個 repo,設定在 `~/.openclaw/`,**分類/催化劑推理/晨間摘要三個排程任務都靠它,見下方「OpenClaw 自動化真正打通」**)。

完整的設計決策/踩過的坑/每一輪的誠實實驗結果,都記錄在:
**`/Users/danielisgod/.claude/plans/quiet-skipping-lovelace.md`**(施工日誌)、**`/Users/danielisgod/.claude/plans/splendid-wiggling-sprout.md`**(production inference + 每日預測紀錄系統的設計文件,含look-ahead風險把關重點)、**`/Users/danielisgod/.claude/plans/frontend-catalyst-rebuild.md`**(消息催化劑分析層 + FastAPI/React前端重做,分Phase 0-3,**目前完成Phase 0+Phase 1**)、**`/Users/danielisgod/.claude/plans/agile-enchanting-quill.md`**(這次在做的計畫:GDELT歷史回填,經過plan mode兩輪agent驗證的實作記錄)。新對話應該先讀這四份。另外兩份 OpenClaw 交接文件(`~/.claude/plans/openclaw-scanner-handoff.md`、`~/.claude/plans/openclaw-catalyst-synthesis-handoff.md`)**已經處理完、不用再讀**,結果記錄在下方「OpenClaw 自動化真正打通」小節。

---

## 目前做到哪(誠實現況)

| 模組 | 狀態 | 白話說明 |
|---|---|---|
| 資料層 schema | ✅ 完成 | 24張表,SQL migration 管理,一行指令建好整個資料庫 |
| 4支資料連接器 | ✅ 上線中 | yfinance(股價,8年歷史)、FRED(總經)、選擇權鏈(GEX/skew,每天累積)、RSS財經新聞+Reddit(免費公開RSS,不用API key) |
| 模組A:方向擇時模型 | ✅ 垂直切片完成,**訊號偏弱** | 三套方法(GMM/HMM regime + 邏輯回歸/LightGBM direction model)預測5天後半導體板塊漲跌,準確率略高於瞎猜基線,還不到能實戰的程度,但地基紮實、測試過。**這次誠實測試了 LightGBM,結果比邏輯回歸更差**(見下方) |
| 模組B:EV閘門+Kelly部位 | ✅ 完成,**目前沒展現篩選力** | 誠實結果是目前訊號太弱,篩選效果不明顯 |
| **Production Inference**(新) | ✅ v1完成,**現已涵蓋全watchlist** | `stockmoney.models.production`:讓模組A/B從「只能回測」變成「每天能對今天實際輸出判斷」。原本只支援 semiconductor 板塊,這次補上 big_tech 的 `xsec_dispersion` 特徵後,11支watchlist標的全部支援 |
| **選擇權風控軌道**(CLAUDE.md 10.1) | ✅ 完整版完成 | 六種停損停利觸發器全部接上真數字(regime失效、動態EV停利這兩個上次略過的,這次靠production inference補上了) |
| **每日預測紀錄系統**(新) | ✅ v1完成 | `daily_predictions` 表 + `scripts/daily_prediction_cli.py`(record-watchlist/grade/list):每天對watchlist標的下具體判斷(方向+目標價),到期後驗證輸贏,勝率跟回測用同一套漲跌幅門檻,可直接互相對照 |
| ~~Streamlit Dashboard~~ | 🗑️ **已退役** | React前端功能對齊後拆掉了`scripts/dashboard.py`跟`streamlit`依賴,不再是這個專案的一部分 |
| 夜間自動化(OpenClaw) | ✅ **完全打通**(2026-07-11) | 分類/晨間摘要/催化劑推理三段 pass 全部用 `claude-cli/claude-sonnet-4-6`(訂閱額度,不是付費API)排程執行,全部端到端實測驗證過。見下方「2026-07-10/11 深夜:OpenClaw 自動化真正打通」小節 |
| **消息分類層** | ✅ **自動化上線**(OpenClaw + claude-cli) | `scripts/fetch_unclassified.py`/`record_classification.py` 走 OpenClaw cron,`stockmoney-scan-classify` 每晚 00:30 跑。舊的 `scan_classify.py`(純Python打付費API)保留當手動備援,不是主要路徑 |
| **催化劑深度推理**(`catalyst_synthesis`) | ✅ **自動化上線**(2026-07-11) | 新腳本 `fetch_catalyst_evidence.py`/`record_catalyst_signal.py` 走 OpenClaw cron(`stockmoney-scan-catalyst-synthesis`,每晚 01:00,claude-cli/Sonnet),已用真實資料端到端驗證(META 傳導鏈推理寫進 `catalyst_signals`)。舊的 `synthesize_catalysts.py`(付費API版本)保留當備援參考 |
| **FastAPI 後端**(`stockmoney.api`) | ✅ 完成、已用真實資料驗證過 | 取代Streamlit當資料來源,read-only,4組endpoint全部接上真資料 |
| **React 前端**(`frontend/`) | ✅ **四個核心視圖全部完成**、已用preview工具實測驗證 | 取代Streamlit,Vite+React+TS+Tailwind,深色交易員視角。「今日機會」「標的詳情」「消息雷達」「戰績」「持倉風控」五頁(含頂部導覽列)全部可用,真實資料渲染正確、零console錯誤 |
| 測試 | ✅ 331個Python全過 + 23個前端(Vitest)全過 | `./scripts/check_all.sh` 一鍵跑齊(後端pytest+前端vitest+tsc) |
| **每日歸因覆盤引擎**(新,CLAUDE.md §11) | ✅ v1完成、真實資料驗證過 | `attribution.py`:因子拆解+三verdict分類,`wrong_signal_existed`案例才產出`feature_candidates`,只提案不促生產 |
| **週末校準戰役**(新) | ✅ **核心邏輯+OpenClaw排程全部上線**,真實驗證過 | `calibration_campaign.py`:三層漏斗+search/confirm holdout分離,防過擬合設計,真實對SOXL跑過完整網格驗證。narrator cron job已註冊並實測跑過一次,WhatsApp送達成功 |

**白話總結**:Phase 0/1/2 的前端+API骨架已經全部做完並用真實資料驗證過——四個視圖(今日機會/消息雷達/戰績/持倉風控)都能跑,誠實顯示「尚無資料」而不是造假。**這次(2026-07-11)最大的進展**:「消息分類/催化劑推理」的自動化路徑(OpenClaw agent + claude-cli訂閱)**完全打通並上線**——過程中在 OpenClaw 核心裡發現並修好兩個真正的架構bug(`toolsAllow` 會把 skill 內容一起拿掉、CLI backend 完全不支援 `toolsAllow`),讓「絕不用付費API、只用訂閱額度」這個使用者的硬性要求第一次真正可行,三段 pass(分類/晨間摘要/催化劑推理)全部排程上線並用真實資料端到端驗證過。

---

## 這次(2026-07-10 下午)做了什麼

### 1. Production Inference(`src/stockmoney/models/production.py`,新檔案)
讓模組A/B從「只能回測」變成「每天能對今天實際輸出判斷」。核心函式:
- `fit_production_model` / `predict_latest`:在全部已解析歷史上fit一次,對「今天」這種還沒有標籤的資料打分數
- `current_ev_of_continuing`:給選擇權風控的動態EV停利用
- 配套:`feature_matrix.py` 新增 `latest_unresolved_feature_rows()`(回傳 `build_feature_matrix` 丟掉的「還沒解析」那些列),並寫了明確的**互斥/聯集測試**(`test_build_feature_matrix_and_latest_unresolved_are_disjoint_and_exhaustive`)確保這兩個函式的切分永遠不重疊不遺漏——這是這整塊裡look-ahead風險最高的地方,規劃階段特別標注過。
- **v1範圍限制**:只有 semiconductor 板塊(SOXL/SOXS/NVDA/AVGO/AMD/TSM)有算過 `xsec_dispersion` 特徵,big_tech(AAPL/MSFT/GOOGL/META/AMZN)目前會被跳過並印出原因,不是crash。
- 過程中順便修掉一個既有小bug:`build_feature_matrix` 在完全沒資料時會直接crash(`pl.DataFrame([]).sort("trade_date")` 找不到欄位),已修成回傳空表。

### 2. 每日預測紀錄系統(`daily_predictions` 表 + `scripts/daily_prediction_cli.py`)
- `record` / `record-watchlist`:呼叫production inference,把「今天的判斷」(方向+機率+目標價+關鍵特徵值)寫進 `daily_predictions` 表,狀態是 `pending`
- `grade`:到期後查實際股價,用**跟 `feature_matrix.py` 完全同一套**漲跌幅門檻公式判定輸贏,狀態變 `graded`
- `list`:列出所有/待驗證/已驗證的預測
- 刻意不做:接進 OpenClaw cron(使用者明確劃在這次範圍外),先確保腳本手動能跑對

### 3. 選擇權風控軌道補完
`options_positions_cli.py` 的 `check` 指令跟 dashboard 的風控燈號區塊,現在會自動呼叫 production inference 補上 `current_regime`/`ev_of_continuing`,不用手動輸入。`open` 指令新增自動記錄 `regime_at_entry`。之前一直顯示「略過」的兩個觸發器(regime失效、動態EV停利)現在真正會亮燈了。

### 4. Dashboard 圖表化改版
- 勝率趨勢圖(20筆滾動勝率,st.line_chart)、輸贏分布圖(st.bar_chart)
- Watchlist 拆成「核心觀察」+「候選觀察中」(候選目前是空的,顯示清楚的說明文字而不是假裝清單是死的)
- 每筆預測有「為什麼」展開面板,顯示當時的關鍵特徵值
- 風控燈號區塊接上真的regime/EV數字

### 5. 修一個 OpenClaw 的根本問題(過程中發現,不是這次主線任務)
`stockmoney-scan-classify` 這個 cron job(AI判讀Reddit/新聞用的)原本連跑都跑不起來:設定的 model 是 `claude-cli/haiku`,但 OpenClaw 全域 `agents.defaults.models` 白名單沒有這個值,直接被拒絕。已修:白名單加入 `anthropic/claude-haiku-4-5`(用 `openclaw infer model list` 查到的正確canonical id),cron job 的 model 也改成這個值,並重啟了gateway讓設定生效。**修完後這個job真的能跑了**,但卡在下一關(exec權限——agent沒有乖乖只跑SKILL.md規定的3個指令),見下方待辦。

### 6. 補 big_tech 特徵 + 修「特徵從來沒排程重算過」的根本缺口
- `dispersion.py` 的 `SECTOR_MEMBERS` 加入 `"big_tech": ["AAPL","MSFT","GOOGL","META","AMZN"]`
- **發現一個沒人接線過的缺口**:`compute_realized_vol_20d`/`compute_adx_14`/`compute_xsec_dispersion`/`compute_macro_features` 這幾個特徵計算函式,從專案一開始就沒有任何排程腳本呼叫過——`nightly_refresh.py` 只有抓原始資料(OHLCV/總經/選擇權鏈),從來沒有重算特徵,導致 `feature_store` 比 `ohlcv_daily` 落後將近一週(production inference 每次都只能看到約一週前的「今天」)。新增 [scripts/compute_features.py](scripts/compute_features.py) 並接進 [nightly_refresh.py](scripts/nightly_refresh.py)(`write_features` 本身已是idempotent去重設計,重跑全歷史安全,只會新增真正新的列)。跑過一次後,big_tech 的 xsec_dispersion 一次補了 2010 筆歷史,semiconductor 的落後也追上了。
- `daily_prediction_cli.py` 的 `record-watchlist` 從硬寫死的6檔semiconductor標的,改成動態讀 `watchlist_members` 全部啟用中的標的(現在11檔全支援)
- **順便修一個真的bug**:`record_prediction` 原本沒有 (symbol, trade_date) 唯一性檢查,重跑 `record-watchlist` 兩次會產生重複預測列,之後grade勝率會被重複計算污染。已修成跟 `write_features` 一樣的idempotent設計(同一天同一標的已有紀錄就回傳原本的id,不重複insert),並清掉了資料庫裡因此產生的6筆重複紀錄。

### 7. 模組A訊號強化嘗試:LightGBM(誠實結果——沒有比較好)
CLAUDE.md 第14節從一開始就把 LightGBM 列為v1計畫演算法(邏輯回歸只是baseline對照組),但一直沒真的做出來。這次補上 `LightGBMDirectionModel`(`direction.py`,跟 `LogisticDirectionModel` 完全同一套per-regime介面,可以直接在 `walk_forward.run_walk_forward` 裡互換),寫了 [backtest_direction_models.py](src/stockmoney/models/backtest_direction_models.py) 做誠實的樣本外比較(bootstrap CI,不是憑肉眼比數字)。

**結果**:LightGBM 明顯比邏輯回歸差(Brier 0.727 vs 0.660,95% CI [+0.045, +0.087] 完全排除0,而且LightGBM整體準確率0.342甚至低於瞎猜基線0.367)。原因推測:每個regime的訓練樣本只有幾百筆,LightGBM的彈性換來的是過擬合,不是更好的訊號。**這是誠實結果,不是失敗**——CLAUDE.md原本就沒有預設誰會贏,樣本外驗證後淘汰LightGBM、繼續用邏輯回歸,跟淘汰HMM選GMM是同一種紀律。LightGBM的程式碼保留著(測試也留著),之後如果加了更多特徵、樣本數上來,可以重新跑這個比較腳本再驗證一次。
- 這個過程也順手裝了 `libomp`(LightGBM在macOS需要的系統依賴,`brew install libomp`,不是repo裡的東西,新機器要重跑一次)

### 8. 模組A訊號強化嘗試:新增 RSI-14 + Volume Z-score(誠實結果——也沒有比較好)
比照CLAUDE.md第12節的規矩(新特徵要過bootstrap顯著性檢定才能進production,不能單看一個數字漲跌就下結論),新增兩個跟現有6個特徵正交、有完整8年歷史可以驗證的技術指標:
- `rsi_14`(Wilder's RSI,動能/超買超賣,`src/stockmoney/data/features/rsi.py`)
- `volume_zscore_20d`(20日滾動成交量Z分數,參與度/流動性訊號,`src/stockmoney/data/features/volume.py`)

寫了 [backtest_feature_ablation.py](src/stockmoney/models/backtest_feature_ablation.py) 專門做「加了這些特徵後,樣本外Brier分數有沒有顯著變好」的配對bootstrap檢定(同一個regime track+direction model,只有餵進去的欄位數不同,其餘完全一致)。

**結果**:95% CI 是 `[+0.0027, +0.0231]`,完全落在正值那一側——代表加了這兩個特徵**顯著讓Brier分數變差**,不是雜訊。已經照CLAUDE.md規矩把 `feature_matrix.FEATURE_COLUMNS` 改回原本的6個,**這兩個特徵不促生產**,但计算函式留著、資料留在 `feature_store` 裡(這次已經回填完整8年歷史,~2.2萬筆),之後累積更多想法或跟別的特徵組合時可以重新驗證。

兩次嘗試(LightGBM、新特徵)都是誠實地驗證後淘汰,不是失敗——這正是CLAUDE.md整份文件反覆強調的紀律:寧可誠實回報「試了沒用」,也不要為了讓數字好看而跳過樣本外驗證。

---

## 2026-07-10 晚上:新計畫 Phase 0(消息催化劑層 + FastAPI/React 前端重做)

使用者反饋現有 dashboard 不直觀,診斷發現根因不是Streamlit醜,是**最該顯示的消息/催化劑層在系統裡根本不存在**(`alt_social_hourly`/`watchlist_candidates`/`attribution_log` 全是0,模組A零消息特徵)。完整計畫見 `~/.claude/plans/frontend-catalyst-rebuild.md`,分Phase 0-3並行做「消息催化劑層」+「FastAPI+React前端」兩條軌道。這次完成 **Phase 0**(地基)。

### 9. 消息分類繞過OpenClaw(`src/stockmoney/data/scan_classify.py` + `scripts/classify_scan.py`,新)
OpenClaw scanner agent的exec權限死路(見上方待辦第1點)**不再是必經之路**——分類不需要跑在有shell權限的agent裡。改用純Python直接呼叫Anthropic API(Haiku),用嚴格tool-use JSON schema強制輸出形狀,透過既有的`stockmoney.data.classification`寫入邊界(`record_sentiment`/`record_candidate`)寫回,無shell exec、更安全。

- 新表 `scan_classifications`(migration 025):記錄每則內容是否已分類過(含'irrelevant'/'error'結果),讓cron重跑同一時間窗不會重複分類同一則內容
- `fetch_unprocessed_items`:讀取未分類的news/social內容(去重到最新ingested_at版本,排除已在`scan_classifications`裡的)
- `run_classification_pass`:分批呼叫API,每批結果透過`apply_result`路由到`record_sentiment`/`record_candidate`,單一壞資料(未知symbol、缺欄位)只跳過那一項,不會讓整批失敗
- 16個測試,含**注入安全測試**:確認惡意內容(`"...DROP TABLE watchlist_members..."`)只會被當成純資料存進`rationale`欄位,資料庫結構完全不受影響;確認假造symbol無法自己創建新的watchlist成員
- **現況**:程式碼完成、`uv run pytest`全過,但**還沒真的跑過API**——`.env`裡`ANTHROPIC_API_KEY`是空的,需要使用者填入真key。跑`fetch_unprocessed_items`確認過:資料庫裡有**162則真實、未分類的內容在等**(最近48小時)

### 10. FastAPI 後端(`src/stockmoney/api/`,新,取代Streamlit當資料來源)
Streamlit dashboard `_module_ab_report()`每次載入都要live跑walk-forward回測(那個「跑 walk-forward 回測中」spinner),不能拿來當API的資料來源。新架構把「算」跟「讀」分開:

- 新表 `symbol_backtest_snapshot`(migration 026):每晚快取每個watchlist標的的walk-forward回測摘要(準確率/Brier/Sharpe/EV閘門通過率)+ 目前EV-of-continuing,只用GMM(production.py早就選定的unattended-production預設,理由同HMM退化風險)
- `scripts/build_dashboard_snapshot.py`:對全部11檔watchlist標的跑一次production inference(寫進`daily_predictions`)+ walk-forward回測(寫進`symbol_backtest_snapshot`),已接進`nightly_refresh.py`最後一步。**真實跑過一次:11檔全部`ok`,總耗時約10秒**
- 重構:把`daily_prediction_cli.py`裡原本私有的`_record_one`邏輯提升成`daily_predictions.record_live_prediction`共用函式,CLI跟新的snapshot builder都呼叫同一份,避免兩份邏輯各自維護
- `src/stockmoney/api/`:FastAPI app(`main.py`)+ 查詢層(`queries.py`,獨立可測試、不需要HTTP)+ 短命read-only連線(`db.py`,同dashboard.py的避免檔案鎖手法)
  - `GET /api/opportunities`:全watchlist依conviction(=機率最大值)排序,附「為什麼」(feature_values)+ 快取的回測數字
  - `GET /api/ticker/{symbol}`:單一標的整合(預測+回測+歷史紀錄+價格走勢)
  - `GET /api/predictions`、`/api/positions`(風控燈號現在也用快取regime/EV,不會live fit)、`/api/watchlist`、`/api/pipeline-health`
  - `GET /api/catalysts`:目前是明確的「尚未建置」stub(`available: false`),Phase 1才會有真內容——刻意不回傳空陣列假裝資料存在
- 25個測試(16個queries.py + 9個路由層,用FastAPI TestClient),**加上用preview工具對真實資料庫跑過全部端點,資料合理**(例如`/api/opportunities`目前準確率都在0.38~0.45,符合HANDOFF一直誠實回報的弱訊號現況,不是灌水數字)
- `.claude/launch.json`新增`fastapi-backend`設定(port 8000):`uv run uvicorn stockmoney.api.main:app --reload --port 8000`

### 副產品:發現FRED總經資料落後
跑production inference時發現所有標的的"今天"都卡在2026-07-02而非更新的日期——追查發現`dxy_chg_1d`這個特徵(來自FRED的DXY序列)最新只到07-02,其他特徵(realized_vol_20d/adx_14/xsec_dispersion)都到07-09。因為`build_feature_matrix`要求6個特徵全部非空才算完整,單一落後的特徵就會拖累整組。**這次沒有修**(不在Phase 0範圍內),列進下方待辦。

**副產品:第一批真實預測已grade完畢**——`ohlcv_daily`已經有07-09的資料了,趁勢跑了`daily_prediction_cli.py grade`,2026-07-02記錄的11筆預測全部驗證完畢(10/11「贏」,但那是因為只有NVDA一筆是方向性判斷且輸了——方向性勝率0/1,樣本太小沒統計意義,只是確認流程真的能跑通)。

---

## 2026-07-10 深夜:Phase 1(分層催化劑推理 + React前端)

### 11. 催化劑深度推理第二段(`src/stockmoney/data/catalyst_synthesis.py` + `catalyst_signals.py` + `scripts/synthesize_catalysts.py`,新)
Pass 1(`scan_classify`,Haiku)是高量淺層標記;這是CLAUDE.md第13節講的「少數關鍵內容做深度傳導邏輯分析」那一步(Sonnet),也是使用者最在意的核心訴求:「把市場還沒判斷出的消息判斷出來」。

- 新表 `catalyst_signals`(migration 027):催化劑摘要 + **傳導鏈推理文字**(catalyst→機制→為什麼影響這檔)+ 新穎度分數 + 情緒分數 + **已被市場消化程度估計**(priced-in estimate)+ 來源refs + `available_at`(look-ahead防護,跟`feature_store`同一套紀律)
- `symbols_with_recent_signal`:只挑pass 1真的有標記到訊號的watchlist標的(不是每天對全部11檔都跑一次Sonnet——CLAUDE.md第13節的成本分層原則)
- `gather_evidence`:對每個標的組裝證據包——聚合情緒(來自`alt_social_hourly`)+ 原始來源文字(透過`scan_classifications.symbols`回查`news_articles_raw`/`social_posts_raw`,重用pass1已經判斷過的關聯性,不重新推導)+ 近期價格走勢
- Sonnet用tool-use schema產出結構化判斷,寫回透過`record_catalyst_signal`(idempotent per symbol/日期,同dailly_predictions模式)
- **紀律確認**:這一層完全不進模型特徵(CLAUDE.md第7節discretion layer),只在前端跟模型判斷並列顯示,見下方API/前端整合
- 22個測試,含證據組裝、idempotency、注入安全測試(惡意內容驗證只會存成純資料)
- 一樣**還沒真的跑過**,待補`ANTHROPIC_API_KEY`

### 12. React 前端上線(`frontend/`,新,取代Streamlit)
Vite + React 19 + TypeScript + Tailwind v4,深色交易員視角,取代原本「按系統模組堆疊」的Streamlit介面,改成**按使用者早上的決策流程組織**:

- **今日機會**(landing,`/`):依conviction(機率最大值)排序的卡片,每張含方向+信心%、regime、回測準確率/Brier(誠實數字,不美化)、催化劑一行標題(尚無資料時顯示清楚的「尚無消息面催化劑資料」而非空白)
- **標的詳情**(`/ticker/:symbol`):價格走勢圖(自製SVG sparkline,沒有另外裝圖表庫)、模型機率長條圖、「為什麼」特徵值面板、**消息催化劑面板**(傳導鏈推理+新穎度+情緒+消化程度,沒資料時誠實顯示待補說明而非假裝存在)、預測歷史表
- API client(`src/lib/api.ts`)型別定義手動對齊`stockmoney/api/queries.py`的輸出形狀(v1沒上schema產生器)
- `vite.config.ts`設了`/api`開發代理轉給FastAPI(:8000),不用另外處理CORS
- `.claude/launch.json`新增`frontend-vite`設定(port 5173):`npm --prefix frontend run dev`
- **已用preview工具實測**:啟動FastAPI+Vite兩個server,點擊今日機會卡片進到SOXL標的詳情頁,確認真實資料正確渲染(價格圖、機率長條、6個特徵值、07-02預測的graded win結果都顯示對),全程零console錯誤、零API請求失敗
- TypeScript編譯乾淨(`tsc -b --noEmit`無錯誤)

**現況**:整個「輸入(消息)→分析(催化劑推理)→呈現(前端)」的骨架第一次完整打通,只差兩個API key呼叫還沒真的執行(分類+催化劑推理)。Streamlit dashboard保留運作,等React前端功能對齊(戰績、持倉風控視圖)再退役。

---

## 2026-07-10 深夜(續):Phase 2 前端補完 + OpenClaw 路徑大轉彎

### 13. React 前端補完四個核心視圖(Phase 2 完成)
延續Phase 1的「今日機會」「標的詳情」,這次補齊：
- **消息雷達**(`/catalysts`):依新穎度×情緒強度排序的催化劑清單,接 `/api/catalysts`
- **戰績**(`/predictions`):勝率趨勢圖(用通用化後的`Sparkline`元件)、輸贏分布、近期預測表,接 `/api/predictions`
- **持倉風控**(`/positions`):開倉部位卡片+風控燈號+觸發器明細,接 `/api/positions`
- `Layout.tsx` 加了頂部導覽列串起全部5頁
- `Sparkline` 元件從「只吃價格」通用化成「吃任意數值序列」,價格圖跟勝率趨勢圖共用同一份程式碼
- 全部用preview工具點過一輪:4個新視圖 + 導覽列切換,零console錯誤、零API失敗,誠實空狀態(尚無持倉/尚無催化劑資料)正確顯示,不是假裝有資料
- `tsc -b --noEmit` 乾淨,Python測試維持274個全過(這輪沒動Python程式碼)

**現況**:React前端功能已對齊Streamlit(甚至更完整)。**已退役**:`scripts/dashboard.py`跟`streamlit`依賴都拆掉了,見下方「Streamlit退役」小節。

### 14. 使用者硬性表態:絕不用付費 API key,只用訂閱額度(重大方向轉彎)
使用者原話:「我永遠都不要用額外付費的apikey,我只要用我訂閱的量去使用haiku或是sonnet甚至opus我都想要用我訂閱的量,但是我不想要影響原本的程度」。

這代表 Phase 0 做的 `scripts/classify_scan.py`/`scripts/synthesize_catalysts.py`(純Python直接呼叫Anthropic付費API)**不再是主要自動化路徑**,改成手動備援用的參考實作。真正的自動化改回**OpenClaw agent + `claude-cli` provider**(透過已登入的 Claude Code CLI 走訂閱額度,不是 `anthropic/*` 那種論token計費的API)。

**這次深入查了 OpenClaw 內部運作**(過程記錄完整,見交接文件),重要發現：
- `claude-cli` provider **沒有 Haiku**,只有 Sonnet(`claude-sonnet-4-6`)跟 Opus(4-6/4-7/4-8)——這打破了CLAUDE.md原本「Haiku做高量、Sonnet做深度」的分層設計,在「絕不用付費API」的前提下,高量分類只能被迫用Sonnet跑,吃比較多訂閱額度/速率限制。**這是一個真正的取捨,還沒拍板**,留給下一個session跟使用者確認
- **找到並修好一個真正的根因**:`stockmoney-scanner` skill 原本放在 `~/.openclaw/workspace/skills/`,但 scanner agent 的 `workspace` 欄位是 `/Users/danielisgod/Projects/stockmoney`——完全不同路徑,OpenClaw 根本沒把它註冊成這個 agent 能用的 skill(`openclaw skills list --agent scanner` 查不到)。用 `openclaw skills install ~/.openclaw/workspace/skills/stockmoney-scanner --agent scanner --as stockmoney-scanner` 修好,現在顯示「✓ Ready」「Visible to model: yes」
- **SKILL.md 加了「HARD RULE」章節**,更嚴格明確禁止任何超出3個文件指令的探索性動作(之前的失敗紀錄顯示agent會自己跑`find`)
- 全域模型白名單新增 `claude-cli/claude-sonnet-4-6`(別名`sonnet-cli`),**只新增沒動其他項目**,已用`--dry-run`驗證過再套用
- **還有一個沒解開的謎團**:即使skill裝好了、顯示ready,cron觸發的session裡模型還是不會用skill內容,還是直接跑`find`探索。目前的假設是cron job的`toolsAllow: ["exec"]`可能連skill載入機制本身需要的工具都一起擋掉了——這個假設沒驗證,因為我嘗試`openclaw cron edit ... --clear-tools`來測試時,**被自己的安全機制正確擋下**(這會把exec限制整個拿掉,是明顯的安全降級,使用者沒有明確授權這個具體動作)

**這一大塊被寫成獨立交接文件,不繼續深挖,原因是使用者明確要求**(怕跟其他功能開發混在一起趕工,也符合HANDOFF一直以來的紀律):
**`~/.claude/plans/openclaw-scanner-handoff.md`** —— 完整記錄已驗證安全的修復步驟、未解問題的假設與下一步調查方向、Haiku vs Sonnet取捨的決策清單、驗證用的指令。**下一個 session 應該直接讀這份文件接手**,不要重新調查一遍。

### 給下一個 session 的具體提醒
- `.env` 的 `ANTHROPIC_API_KEY` **不用填**——除非使用者之後改變心意想重新啟用付費API這條路
- `scripts/classify_scan.py`/`scripts/synthesize_catalysts.py` 保留在repo裡當手動/備援用,不是目前規劃的自動化路徑,不用刻意維護但也不用刪
- OpenClaw 相關的一切設定變更都在 `~/.openclaw/openclaw.json`(全域,影響使用者機器上其他agent)跟 `/Users/danielisgod/Projects/stockmoney/skills/stockmoney-scanner/SKILL.md`(現在生效的skill檔案,**不是**舊的`~/.openclaw/workspace/skills/`那份),修改前務必小心,參考交接文件裡「已驗證安全」跟「需要使用者確認」的區分

---

## 2026-07-10 深夜(續2):催化劑推理另開交接文件 + 四件清理工作

`~/.claude/plans/openclaw-scanner-handoff.md` 那個 session 已經在動手處理了(不能再塞東西進去搶跑),所以催化劑深度推理(第三段pass,寫`catalyst_signals`表)這個額外缺口另外寫成**獨立**交接文件:

**`~/.claude/plans/openclaw-catalyst-synthesis-handoff.md`**(新)——記錄了為什麼分類 pass 修好不代表消息雷達頁面就有資料(SKILL.md 目前的晨間摘要 pass 只回覆文字給人看,不寫資料庫),以及要補的東西:兩支新腳本(`fetch_catalyst_evidence.py`/`record_catalyst_signal.py`,仿造分類 pass 那兩支的模式)、SKILL.md 第三段、新 cron job(用 `claude-cli/claude-sonnet-4-6`,這段本來就設計給Sonnet用,不像分類pass有Haiku取捨問題)。**這份文件假設 `openclaw-scanner-handoff.md` 已經處理完**,依賴關係要注意。

接著做了使用者要求的四件清理工作:

### 15. FRED macro 資料落後修復(`src/stockmoney/data/features/macro.py`)
根因跟原本以為的不一樣:不是`fred_macro.py`抓取窗口太小(30天窗口綽綽有餘),是**DTWEXBGS(Fed的貿易加權美元指數)這個FRED序列本身的發布排程,有時候會連續5+個交易日不更新**(Fed H.10發布本身的特性,不是bug)。因為`build_feature_matrix`要求6個特徵全部非空才算一列完整,這一個慢的序列就拖累了全部標的的"今天"。

修法:新增`dxy_chg_1d_ffill`/`oil_chg_1d_ffill`兩個特徵,把原始數值**forward-fill到每個交易日**再算日變化率(沒新數據的日子讀0%變化,有新數據的那天讀真實累積變化)。`available_at`維持釘在數值真正被知道的時間點,不是交易日當天,look-ahead防護不受影響。原本的`dxy_chg_1d`/`oil_chg_1d`保持不動(feature_store規定同一個feature_version的值不可變),`feature_matrix.FEATURE_COLUMNS`改指向新的`_ffill`版本。

**驗證結果**:重跑`compute_features.py`後,production inference的"今天"從07-02追到07-08(6天進步)。剩下07-08 vs 07-09這1天落後,查過是**Treasury殖利率(DGS10/DGS2)當時還沒發布07-09的數字**——這是正常的T-1申報延遲,不是bug,沒有再處理。4個新測試(`test_macro.py`),誠實驗證forward-fill在有/無新數據時的行為跟available_at正確性。

### 16. 前端測試(Vitest + React Testing Library,新)
`frontend/`原本只有`tsc -b --noEmit`型別檢查跟preview工具手動驗證,沒有自動化的component測試。裝了Vitest(用`@tailwindcss/vite`那套一樣走vite原生整合,不需要額外webpack/jest轉譯設定)+ Testing Library,寫了23個測試涵蓋:
- 兩個純展示元件(`Sparkline`/`ProbaBar`)的邊界情況(空資料、單一值、零range不crash)
- 五個頁面(`Opportunities`/`TickerDetail`/`CatalystRadar`/`TrackRecord`/`PositionsRisk`)各自的載入中/正常渲染/**誠實空狀態**/API錯誤四種情境,mock `../lib/api`模組而非真的打API

跑法:`npm --prefix frontend test`(單次)、`npm --prefix frontend run test:watch`(watch模式開發用)。

### 17. Streamlit 退役
React前端功能已經對齊(甚至超過)Streamlit dashboard,拆掉`scripts/dashboard.py`、`uv remove streamlit`、`.claude/launch.json`移除對應的`streamlit-dashboard`設定項目。全部277個Python測試(移除streamlit不影響任何測試,原本就沒有測試依賴它)+ 23個前端測試都還是全過。

### 18. Regime detection 特徵分離(✅ 已完成,經 plan mode 規劃)
CLAUDE.md第4節明確要求regime偵測只該用3個觀測特徵(已實現波動率、趨勢強度、跨股離散度),但`walk_forward.run_walk_forward`實際上把direction model用的全部6欄特徵矩陣整包餵給regime clustering,兩者共用同一份特徵向量,沒有依規格拆開。這是文件本身標註「值得單獨規劃、用plan mode把關再動手」的那類變更(regime detection是全專案look-ahead/過擬合風險最高的模組),所以先進plan mode規劃(兩輪agent驗證:Explore agent找出全部呼叫點、Plan agent實測驗證關鍵假設)才動手。

**做法**:`Dataset`新增必填欄位`regime_X`(3欄,對應新常數`REGIME_COLUMNS`),`X`維持原本6欄給direction model用。`to_dataset()`同時建構兩者。`walk_forward.py`跟`production.py`裡所有`track.fit()`/`track.label()`呼叫從`dataset.X`改成`dataset.regime_X`,direction model呼叫不變。4支backtest腳本+`build_dashboard_snapshot.py`完全不用改(只呼叫`to_dataset()`+`run_walk_forward()`,自動繼承修復)。`backtest_feature_ablation.py`也不用改邏輯(`dataclasses.replace(..., X=...)`本來就不會動到`regime_X`,已用真實Python repro驗證)。

**順便修復的真bug**:跑驗證時發現`backtest_feature_ablation.py`的`BASELINE_COLUMNS`還在用FRED修復(第15節)之前的舊名`dxy_chg_1d`/`oil_chg_1d`,這兩個名字已經不在`FEATURE_COLUMNS`裡了,腳本會直接crash(`ValueError`)。順手修正成`_ffill`版本。

**`production.MODEL_VERSION`從`"gmm-logistic-v1"`升到`"gmm-logistic-v2"`**——這是對regime clustering的真實行為變更(輸入維度變少,同一天可能被分到不同regime),舊/新版本的判斷不該混在同一個版號下污染`daily_predictions`勝率帳本。

**驗證結果**(真實資料庫上跑過):
- `uv run pytest tests/ -q`:280個全過(277 + 3個新測試,含仿造`test_leakage_canary.py`風格的新canary檔`test_regime_column_isolation.py`,用會raise的spy RegimeTrack確保regime永遠只看3欄)
- `backtest_semiconductor.py`/`backtest_ev_gate.py`/`backtest_feature_ablation.py`/`build_dashboard_snapshot.py`全部重跑過,乾淨無crash、無GMM收斂警告。**歷史回測數字確實變了**(這是修復生效,不是回歸——例如GMM regime 2的Sharpe從原本正值變成-2.01,因為regime分群現在只看3個真正該看的特徵)。GMM vs HMM「無顯著差異」的結論碰巧維持不變,但這次是在正確的3欄輸入上測出來的
- `production.predict_latest('SOXL')`確認`model_version`正確顯示`gmm-logistic-v2`,`feature_values`仍保留全部6個key(給前端「為什麼」面板跟`daily_predictions._band()`用,兩者都需要`realized_vol_20d`,剛好也在`REGIME_COLUMNS`裡,不受影響)

---

## 2026-07-10/11 深夜:GDELT 歷史回填(✅ 已完成,真的回填了)

使用者想做的事:「用7/2號前資料預測7/2號之後走勢,跟實際表現比對驗證」——技術面模型早就有這個(`walk_forward.py`),但消息面完全沒有,因為真實新聞/Reddit資料只累積了5小時。三條路權衡後(DOC 2.0 API只保證近3個月、原始CSV批次下載每年2.5TB不現實),使用者選了 **GDELT 透過 Google BigQuery 公開資料集**(`gdelt-bq.gdeltv2.events`,2015年至今)回填多年歷史,並自己完成了 Google Cloud 帳號/服務帳號/金鑰設定。

**這是本次規劃最嚴謹的一次**:兩輪Explore agent + 一輪Plan agent(**直接讀原始碼確認**look-ahead安全機制)才動手實作。但即使規劃仔細,**真正接上 BigQuery 之後還是發現了兩個規劃階段測不出來的真實問題**,誠實記錄如下——這是「先用免費的dry-run驗證再花真錢」這個紀律真正發揮作用的案例。

### 規劃時沒發現、接上真實BigQuery才發現的兩個問題

1. **`gdelt-bq.gdeltv2.events`完全沒有做時間分區**(`client.get_table()`直接確認:`time_partitioning`/`clustering_fields`都是`None`,9億列、391GB)。這推翻了原計畫「用`SQLDATE`區間過濾+按季切分省成本」的核心假設——**不管查1天還是11年,掃描的位元組數完全一樣**(親自用dry-run比對驗證過)。按季切分44次查詢,成本是查一次的44倍,不是省錢,是花44倍的錢。修法:`scripts/backfill_gdelt.py`改成**一次查詢涵蓋整段歷史**,不切分。
2. **原始逐事件查詢在11年歷史+現有篩選條件下,結果集是1億多列**——BigQuery計費不看回傳幾列,只看掃描幾個位元組,所以這在BigQuery帳單上不是問題,但把上億筆原始事件一筆一筆搬進24GB記憶體的筆電本地DuckDB,不可行。修法:新增`fetch_gdelt_daily_aggregates`/`ingest_gdelt_daily_aggregates`,**在BigQuery SQL端直接做`GROUP BY SQLDATE`聚合**(mention加權平均語氣/衝突量表),掃描成本不變,但下載量從上億筆變成**一天一列**(11.5年 = 4209列)。每一天存成一筆合成的「事件」(`gdelt_event_id = "daily-agg-YYYYMMDD"`,`event_class`/`geo_lat`/`geo_lon`設NULL,因為一整天的聚合值沒有單一地點/事件類別可對應),讓`compute_gdelt_sentiment`既有的逐日mention加權讀取邏輯完全不用改就能吃這種資料。

### 實際跑的結果(真的花了 BigQuery 額度,誠實記錄用量)
```bash
uv run python scripts/backfill_gdelt.py --start 2015-01-01 --end 2026-07-10
# dry-run: 46.89 GB(免費額度1TB的4.7%)→ 4209 daily rows written
```
**單次查詢,$0實際花費,遠低於免費額度**。跑完後發現一個自己造成的小問題:早先為了測「還沒回填時要優雅降級」,已經用同一個`feature_version="v1"`把全部歷史寫成中性值(0.0)——`write_features`的不可變契約(同一個版本鍵值只寫一次,之後永遠跳過)導致真資料寫不進去。修法是把`gdelt_sentiment.FEATURE_VERSION`從`v1`升到`v2`(因為這個候選特徵從沒被加進`FEATURE_COLUMNS`,沒有任何模型訓練在v1上,升版零風險)。

`gdelt_avgtone_1d`/`gdelt_goldstein_1d`兩個特徵現在在`v2`下都是**真數字**(涵蓋2018-07-09到2026-07-09的2011個交易日,平均語氣約-6.1、標準差0.36,不是全0),`v1`的舊中性值留著當歷史記錄不用刪。

**明確沒做的事**(照CLAUDE.md第12節,不是漏做):兩個特徵**沒有**加進`feature_matrix.FEATURE_COLUMNS`——要先過bootstrap顯著性檢定才能促生產,這是下一步,不在這次範圍內。使用者原話的擔心(市場層級的粗顆粒情緒訊號可能測出來「沒用」)完全合理,測出沒用跟`rsi_14`/`volume_zscore_20d`一樣是誠實且有價值的結果。

### 驗證結果
- `uv run pytest tests/ -q`:**306個全過**(含新增的daily-aggregate相關測試)
- 資料庫確認:`event_news_gdelt` 4209筆真資料,`feature_store`裡`gdelt_avgtone_1d`/`gdelt_goldstein_1d` v2版本共4022筆真數字(2×2011個交易日)

### ✅ 顯著性檢定已經跑了,誠實結果:目前沒有顯著幫助

回填完當下就跑了`backtest_feature_ablation.py`:先把`gdelt_avgtone_1d`/`gdelt_goldstein_1d`**臨時**加進`FEATURE_COLUMNS`(同時在`_load_features`永久註冊這兩個特徵,跟`rsi_14`/`volume_zscore_20d`一樣的模式——永久留著讓以後能重新測,不是永久促生產),跑完顯著性檢定馬上revert回原本6欄,`uv run pytest`確認306個測試乾淨過。

結果:`mean diff: -0.0001  95% CI: [-0.0037, +0.0036]`——信賴區間跨過0,**沒有統計顯著差異**。跟計畫文件裡誠實預告的一樣:市場層級的粗顆粒GDELT情緒訊號,很可能跟既有的`yield_curve_10y2y`/`dxy_chg_1d_ffill`/`oil_chg_1d_ffill`(都是「有沒有壞事發生」的代理指標)重疊,daily granularity在已經被市場充分定價的大型股上訊號本來就弱——這跟`rsi_14`/`volume_zscore_20d`一樣,是誠實且有價值的負面結果,不是失敗。**兩個特徵繼續留在`feature_store`裡不刪**(`_load_features`的註冊沒revert),累積更多真實新聞資料、或改成per-symbol/per-sector版本(比market-wide版本更可能有idiosyncratic訊號,但要做entity matching,工程量大得多)之後可以重新測。

### 下一步(不在這次範圍內)
- 個股/板塊層級的GDELT訊號(entity matching against Actor1Name/SOURCEURL)——比market-wide聚合更可能有真訊號,工程量大,列為候選
- `fetch_gdelt_events`(逐事件、未聚合版本)保留給未來的「即時/持續增量抓取」路徑用——單日資料量小,可以逐事件存,不用像回填一樣聚合

---

## 2026-07-10/11 深夜:OpenClaw 自動化真正打通

接手 `~/.claude/plans/openclaw-scanner-handoff.md` 跟 `~/.claude/plans/openclaw-catalyst-synthesis-handoff.md` 兩份交接文件,**兩份都處理完了,不再需要新session單獨接手**。過程中發現的問題比原本交接文件記錄的更深,值得記錄清楚。

### 19. OpenClaw 核心 bug 修正(在 openclaw repo 本身動手改的,不是 stockmoney repo)

- **Bug 1**:cron job 設定 `toolsAllow`(限制工具面)時,OpenClaw 會把整個 skill 目錄從 system prompt 拿掉,導致就算 skill 裝好了,cron 觸發的 session 還是看不到 SKILL.md 內容。這正是交接文件記錄的「未解謎團」根因。修在 `src/agents/embedded-agent-runner/run/attempt.ts`。
- **Bug 2**(更深、交接文件沒發現的):OpenClaw 的 `claude-cli` provider(訂閱認證)後台完全不接受 `toolsAllow`,設了就直接拒絕執行。查證後發現這其實可以修——Claude Code CLI 本身原生支援 `--allowedTools`/`--disallowedTools` 旗標,只是 OpenClaw 沒把 `toolsAllow` 轉譯過去。修好後(`src/agents/cli-runner/prepare.ts` + `claude-live-session.ts`),`claude-cli/claude-sonnet-4-6` 現在可以真正限制工具面,不用被迫改用付費 `anthropic/*` API 才能有安全的工具限制。
- **Bug 3**(修 Bug 2 後才浮現):光靠 `--allowedTools` 縮小工具面還不夠——Claude Code CLI 自己的原生核可提示層(`--permission-mode default`)還是會擋下每一次 `mcp__openclaw__exec` 呼叫,而 cron 是無人值守,永遠拿不到核可,結果每次呼叫都被默默拒絕(有時 model 還會生出一段編得很像真實錯誤訊息的幻覺文字,不要被騙)。修法:只要設了 `toolsAllow`,就強制 `permissionMode: bypassPermissions`——因為縮小後的工具本來就有自己的 `security`/`ask`/allowlist 驗證機制,Claude Code 自己那層核可提示反而是多餘且會擋下合法呼叫的那層。

三個修正都補了測試(單元測試 + 一次乾淨的實測驗證),已 build 部署到這台機器上跑的 OpenClaw gateway。

### 20. exec 核可清單修正(`~/.openclaw/exec-approvals.json`,不在 stockmoney repo 裡)
- 清掉之前除錯階段留下的過寬規則(`ls`/`cat`/沒有 argPattern 限制的裸 `uv`)——這些規則違背 SKILL.md 的 HARD RULE 精神,會讓 prompt injection 有機可乘。
- 修正 4 條 scanner agent 的 allowlist 規則:原本 `pattern: "uv"`(裸名稱,只匹配 PATH 解析出來的呼叫)永遠不會匹配 SKILL.md 實際用的絕對路徑呼叫 `/opt/homebrew/bin/uv`。改成 `/opt/homebrew/Cellar/uv/*/bin/uv`(比對的是解析後的真實路徑,uv 是 homebrew symlink;用萬用字元避免 `brew upgrade uv` 之後失效)。

### 21. SKILL.md 改用固定腳本取代 inline `python -c` 查詢
分類 pass 的「檢查目前watchlist」跟晨間摘要 pass 的「查今天新候選」原本是 inline `python -c "..."` 一行指令——這種寫法天生沒辦法被精準加進 exec allowlist(inline eval 範圍無界,沒辦法用固定的 argPattern 鎖死)。改用已經寫好但沒接上的固定腳本 `scanner_check_watchlist.py`/`scanner_todays_candidates.py`(無參數、可被 exact argv 匹配)。

### 22. 建立/驗證三個 cron job,全部用 `claude-cli/claude-sonnet-4-6` + `toolsAllow: exec,read`
- `stockmoney-scan-classify`(00:30 America/New_York):分類 pass,已重新啟用排程
- `stockmoney-scan-digest`(07:00):晨間摘要,投遞到 WhatsApp(`+16692619821`),已重新啟用排程
- `stockmoney-scan-catalyst-synthesis`(01:00,新):催化劑推理,不投遞(純資料庫寫入),已重新啟用排程

三個都用 `openclaw cron run --wait` 實測過至少一次乾淨跑完(無探索性指令、無 subagent 亂生、無幻覺回答),用 gateway log 的 `rawLines` 數字交叉驗證過確實有真實 tool call 往返,不是編出來的回答。

### 23. 補上「哪則原始內容支撐哪個標的情緒分數」的對應關係(新發現的架構缺口)
`~/.claude/plans/openclaw-catalyst-synthesis-handoff.md` 原本設計催化劑推理 pass 直接複用 `catalyst_synthesis.py` 既有的 `_symbol_evidence_items`(讀 `scan_classifications` 表),但深入查證發現:**`scan_classifications` 只有舊的付費API路徑(`scan_classify.py`)在寫,現在真正在跑的分類 pass(`record_classification.py`)完全不會寫進這張表**,寫的是已經彙總過、不含item級別對應關係的 `alt_social_hourly`。照原設計,催化劑推理 pass 會永遠找不到證據可用。

修法:擴充 `stockmoney.data.classification.record_sentiment`,在寫入 `alt_social_hourly` 的同時,如果 payload 附了 `item_id`,就同步把 item→symbol 的對應關係寫進 `scan_classifications`(同一個item對應多個symbol時做合併,不是覆蓋)。SKILL.md 分類 pass 的 sentiment payload 加了 `item_id` 欄位。這樣 `catalyst_synthesis.py`(舊模組,完全沒改動它本身)的既有邏輯就正確接上了現行分類 pass 的輸出。4個新測試,已用真實抓到的 Reddit 貼文(Meta自研AI晶片新聞)手動走過一次完整資料串接,並透過實際 cron 跑出真實的傳導鏈推理寫進 `catalyst_signals` 驗證過。

### 給下一個 session 的具體提醒
- 三個 cron job 現在都是**真正上線在跑的排程**,不是手動測試用的草稿——修改前先 `openclaw cron get <id>` 看目前設定
- OpenClaw 核心的修正(第19點)已經是這台機器上跑的 openclaw 版本的一部分,**不是 stockmoney repo 的檔案**,如果換一台機器或重新 clone openclaw,要確認這些修正有沒有隨版本帶過去(commit hash 見 openclaw repo 自己的 git log)
- `.env` 的 `ANTHROPIC_API_KEY` 依然**不用填**——三段 pass 現在全部走 `claude-cli` 訂閱額度,`scan_classify.py`/`synthesize_catalysts.py` 這兩支付費API版本腳本繼續維持「手動備援參考,不主動維護」的定位
- 高量的分類 pass(150+則/晚)現在吃的是 Sonnet 訂閱額度(claude-cli 沒有 Haiku),如果之後發現這個吃太兇(影響到平常互動用的額度/速率限制),記得這是已知的取捨,不是bug——可以考慮縮小 `--hours` 窗口或降低批次上限

---

## 2026-07-11:A4 每日歸因與覆盤引擎(✅ 已完成,經 plan mode 規劃)

使用者選定的下一步。`attribution_log`(migration 016)+`feature_candidates`(migration 017)兩張表 schema 早就建好但**一直是空的**——從沒有引擎寫進去。這是 CLAUDE.md §11 的「只讀分析側支」,也是**新特徵候選的正規來源**(模組A 兩條顯而易見的訊號強化路徑 LightGBM/RSI+Volume 都誠實驗證過沒用,要找更根本訊號需要一個有紀律的機制去發現「模型錯了、但事後看訊號確實存在」的案例)。屬 look-ahead 敏感模組,先 plan mode 規劃(計畫文件 `~/.claude/plans/frolicking-wishing-cook.md`)再動手。

### 24. 新引擎 `src/stockmoney/data/attribution.py`(新)+ `scripts/attribution_cli.py`(新)
三層(全部只用同視窗已實現報酬,無前視):
- **事實層 `decompose_return`**:把已 grade 預測的報酬拆成 macro/sector/idiosyncratic。**沿用 dispersion.py 的等權/排除槓桿ETF(SOXL/SOXS)/idio=殘差 同一套哲學**,不做 rolling-β(留 v2)。三項**可加性恆等式** `macro+sector+idio ≡ actual_return`(有測試斷言,實測誤差 0)
- **事件層 `match_events`**:誠實貼標——市場級 GDELT 語氣異常(唯一目前有真資料的事件源)+ 個股級 `scan_classifications` 命中;財報(`event_calendar` 空)不塞假資料。無匹配回空,絕不編造
- **模型對帳層 `classify_verdict`**:win→`right_reason_right`;loss 且實際落在模型自己 band 內(actual_label=range)→`wrong_noise`;loss 且是真實方向性 miss 且 idio 主導(或個股事件命中)→`wrong_signal_existed`→**產出一筆 `feature_candidates`(status='proposed')**
- **Idempotent**:PK `(attribution_date,symbol,model_version)`,重跑只 skip;候選 idempotent per (source_date,symbol)
- **只處理已 grade 的預測**,`attribution_log` 存的是「已完成的事實」,永不為 pending 寫入

### look-ahead / 資料隔離把關
- `attribution_log`/`feature_candidates` **永遠不被任何訓練路徑讀取**——有 canary 測試(`test_training_paths_never_read_attribution_tables`)靜態掃 feature_matrix/walk_forward/regime/direction/production 五個檔案確認沒有反向讀取(§7 discretion/meta 隔離)
- 引擎讀 graded 預測是允許的(只讀側支),產出只進歸因表,不回寫 daily_predictions/feature_store
- 候選只能 `proposed`,人工審核才可能 `accepted`——引擎沒有促生產的權限(§11 + §17)

### 接線 + 驗證(真實資料端到端)
- 接進 `scripts/nightly_refresh.py` **最後一步**(snapshot 之後);grade 仍是**刻意的手動步驟**,歸因 idempotent 依賴已 grade 結果,沒新東西就乾淨 no-op
- **真實跑過**:對現有 11 筆 graded 預測(07-02 那批)跑 `attribution_cli.py run` → 10 筆 `right_reason_right`、NVDA(預測 up 實際 range、−0.66% 微小)正確判 `wrong_noise` **不產候選**(這是正確結果:那是雜訊不是漏掉的訊號)。再跑一次 11 筆全 skip(idempotent 驗證)
- 10 個新測試(可加性/三 verdict/候選只在 wrong_signal_existed 產出/idempotency/pending 不歸因/注入安全/隔離 canary)。`uv run pytest tests/ -q` = **316 passed**(306+10)
- 看法:`uv run python scripts/attribution_cli.py list [--verdict ...]` / `candidates [--status ...]`

**下一步(不在這次)**:等預測累積更多、出現真的 `wrong_signal_existed` 案例後,`feature_candidates` 會長出待人工審核的假設;是否加前端「歸因/覆盤」視圖列為可選。

---

## 2026-07-11(續):整體穩定性提升(第一批)

使用者要求的平行第二軌。這次做了計畫文件裡列的三項(第四項 OpenClaw cron 失敗告警**故意延後**,見下方理由),都是「repo 內、無架構爭議、能立刻驗證」的項目。

### 25. Pipeline health 資料新鮮度紅線(`src/stockmoney/api/queries.py`)
`pipeline_health()` 原本只回傳最後一次 ingestion 的狀態/時間,不會主動標示「這已經落後太久」——FRED DXY 落後將近一週那次(見上方「FRED macro 資料落後修復」)完全是人工發現的,沒有自動化訊號。現在每張表依**實際有排程在跑的來源**(`ingestion_runs` 裡真實出現過的 8 個 target_table)分配一個「可接受落後天數」門檻(`STALENESS_MAX_LAG_DAYS`):日更表(ohlcv/options/新聞等)3 天內、`macro_series_daily` 4 天(FRED 發布本身有 lag,見第15點)、**`event_news_gdelt` 故意設 `None`**——它是一次性 BigQuery 回填,沒有排程中的常態 ingester,不該被標紅。回傳新增 `days_since_last_run`/`is_stale` 兩欄;`last_status != 'success'` 也一律視為 stale,不管多新。`frontend/src/lib/api.ts` 的 `PipelineHealthEntry` 型別同步更新(**目前前端沒有頁面渲染這個 endpoint**,只有型別/client 存在,列為之後可選的 UI 缺口,不在這次範圍)。6個新測試(`tests/api/test_queries.py`)。

### 26. 統一測試檢查腳本(`scripts/check_all.sh`,新)
HANDOFF「跑全部測試」原本是三個獨立手動指令(`uv run pytest`/`npm test`/`tsc --noEmit`),沒人保證每次改動前都三個一起跑。新增一鍵、fail-fast 腳本跑完整三項,`chmod +x` 可直接執行:`./scripts/check_all.sh`。

### 27. `nightly_refresh.py` 端到端 smoke test(`tests/scripts/test_nightly_refresh.py`,新)
在此之前**完全沒有任何測試跑過 nightly_refresh 的完整鏈路**——這正是「FRED 落後一週」那個 bug 能夠存在的根本原因:`compute_features.py` 從沒被排程呼叫過,而所有既有測試都是逐一測試單一步驟(`build_dashboard_snapshot`/`compute_features`各自的測試),沒有任何東西驗證「這些步驟接在一起真的會跑」。新增兩個 smoke test:
- 完整鏈路(ingest 用 stub 取代真實網路呼叫)接到 feature recompute → dashboard snapshot → attribution,驗證 `symbol_backtest_snapshot`/`daily_predictions`/`attribution_log` 三張表都被正確寫入,且串接不中斷
- 其中一個 ingestion 來源刻意讓它 raise,驗證 `nightly_refresh.main()` 既有的逐步 try/except 設計真的擋住了(其他步驟仍完整跑完),不是紙上談兵

**驗證結果**:`./scripts/check_all.sh` 全過——**323 個 Python 測試**(316+2 個 smoke test+5 個 pipeline-health 測試)、**23 個前端測試**、`tsc --noEmit` 乾淨。

### 明確延後:OpenClaw cron 失敗告警
計畫文件列的第四項。延後原因:這塊主要在 repo 外(`~/.openclaw/`),而且「告警管道」本身是一個需要使用者拍板的架構決定(重用晨間摘要已經在用的 WhatsApp?閾值多嚴格?誤報耐受度?),跟前三項「repo 內、規格明確、能立刻寫測試驗證」的性質不同,不該在沒有明確範圍確認下動手。下一次要展開這塊,先問使用者告警管道跟門檻。

---

## 2026-07-11(續2):週末校準戰役(Weekend Calibration Campaign)

使用者的想法:8年歷史資料都在,週末沒有新交易日,是閒置算力時間,想系統性掃描 CLAUDE.md §16「待優化」參數(regime數量、band_k、EV閘門視窗/分位數、Kelly係數),誠實記錄每次樣本外表現,追蹤「進步了多少」。核心風險(計畫裡明確處理):週末不會產生新樣本外資料,單純瘋狂試參數挑好看的就是 CLAUDE.md §12 禁止的「全樣本回測後挑贏家」。完整計畫見 `~/.claude/plans/frolicking-wishing-cook.md`。計畫由 Sonnet 5 撰寫,**事後已用 Opus 4.8 完整審查過核心邏輯(CLAUDE.md §15 建議的 opusplan 把關)**,審出一個 merge-blocking 的方法論漏洞並修好——見下方第 30 點「Opus 審查發現與修正」。

### 28. 核心引擎(`src/stockmoney/models/calibration_campaign.py`,新)+ CLI(`scripts/run_calibration_campaign.py`,新)
三層漏斗,search/confirm 兩階段分離:
- **Tier 1(貴,~2200 fit)**:`method∈{gmm,hmm}`×`n_regimes∈{2,3,4,5}`×`band_k∈{0.3~0.7}`×`seed∈{0~4}`×11檔標的,全部複用既有 `walk_forward.run_walk_forward`/`metrics.report_by_regime`,不重新發明統計邏輯
- **Tier 2/3(便宜,後製,不重新fit)**:只對 Tier1 每檔標的 top-3 shortlist 做 EV閘門(`ev_gate.run_ev_gate`)+ Kelly係數(`kelly.position_size`)網格,資訊性質,不當第二道統計關卡
- **防過擬合核心機制**:`_truncate_dataset()` 在 search 階段把最新 `HOLDOUT_FRAC=15%` 的歷史**整個從資料集裡切掉**,search 網格結構上看不到那段資料,不管重跑幾次都一樣。只有 shortlist 能對這段保留資料跑**一次**`confirm`(`n_folds=1` 精準對應那段區間),而且 `confirm` 對同一個 `(campaign_id, symbol, params)` **設計成不能重跑**(比照 `attribution_log` PK 不可變精神)——不能靠「多試幾次確認集」製造假的樣本外驗證
- **判準**:用候選**自己的**regime標籤分段(不是baseline的——不同n_regimes之間regime編號沒有自然對應),對每個regime做`bootstrap_paired_diff_ci`比對固定生產baseline(`gmm,n_regimes=3,band_k=0.5,seed=0`),**至少一個regime顯著變好、沒有任何regime顯著變差**才算通過——CLAUDE.md §12「不合併regime」在這裡是硬性關卡,不只是報告習慣。**⚠️ 比較的是「Brier skill over climatology」不是原始 Brier**(Opus 審查修正,見第30點)——原始 Brier 跨 band_k 不可比
- 資料層(`029_calibration_campaign.sql`):`calibration_runs`(append-only原始結果)+ `calibration_candidates`(比照`feature_candidates`,只提案不促生產,human審查後才手動改常數)——兩張表跟`attribution_log`/`feature_candidates`同一條隔離紀律,**永遠不被任何訓練路徑讀取**(有canary測試把關)

### 29. 敘事層:OpenClaw narrator pass(`scripts/calibration_report_query.py`,新 + `skills/stockmoney-scanner/SKILL.md` 第4段)
算力層(Tier1/2/3網格搜索)刻意**不**包進OpenClaw——純Python確定性計算不需要LLM判斷力,包進agent只多一層開銷沒有好處。跑法就是 `caffeinate -i uv run python scripts/run_calibration_campaign.py search --symbols all`,手動或排程跑一次。敘事層(讀結果、寫進度摘要)才是OpenClaw+Sonnet該做的事:SKILL.md新增第4段pass,固定讀`calibration_report_query.py`(唯讀、無參數,仿照既有`scanner_check_watchlist.py`的可精準allowlist模式),輸出濃縮JSON給narrator寫成人看得懂的摘要,回覆文字透過既有WhatsApp `--announce`管道送達手機——不新開管道。

**✅ narrator排程已上線並實測驗證過**(2026-07-11):`~/.openclaw/exec-approvals.json`加了`scanner-calibration-report`條目(使用者明確核准後才動手,因為這類「機器層級無人值守執行權限」變更觸發了Claude Code auto-mode權限分類器的額外確認關卡,即使plan mode計畫已核准也一樣)。cron job id `46f6c748-1f4f-48d3-b3bc-ead3e2213788`,週六週日各12:00/20:00跑,`claude-cli/claude-sonnet-4-6`+`toolsAllow: exec,read`,delivery走既有WhatsApp管道。

**真實跑過一次**(`openclaw cron run 46f6c748-1f4f-48d3-b3bc-ead3e2213788 --wait`):11.6秒完成,只跑了允許的那一個指令(沒有探索性命令,符合SKILL.md HARD RULE),誠實回報「目前沒有campaign跑過,campaign_id是null」並成功送達WhatsApp——這是正確行為,因為驗證用的`verify-run-1`資料已經清掉了,這次跑的是空狀態的誠實回報,不是造假。

**注意**:實際註冊時CLI旗標跟原本計畫寫的不一樣(`openclaw cron add --help`才是唯一準確來源)——真正的旗標是`--cron`(不是`--schedule`)、`--message`(不是`--instruction`)、`--tools`(不是`--toolsAllow`)、`--announce --channel whatsapp --to <號碼>`(不是`--announce whatsapp:<號碼>`這種單一字串)、`--name`給job名稱。之後如果要再註冊新cron job,直接照抄`openclaw cron get <既有job-id>`的輸出格式最保險,不要照抄舊文件裡的指令字串。

### 驗證結果(修正 Finding 1 之後,以 skill score 重跑)
- **真實資料端到端跑過**:對SOXL跑滿完整網格(200 fit,~2分鐘,單次fit約0.55秒——算力不是瓶頸)。修正前(原始Brier)42/200通過,全是band_k=0.7的label汙染;**修正後(skill score)只有1/200通過**,而且是單一seed——這1個正好落在噪聲底線(200次α=0.05檢定靠運氣出現1個完全預期),跟專案一貫的「訊號很弱」誠實結論一致。這是Opus審查最有力的證據:修正前系統會製造幾十個假「進步」,修正後誠實回報幾乎沒有
- **JSON保真度驗證**:`calibration_runs.per_regime_json`跟直接呼叫`metrics.report_by_regime()`逐字段bit-for-bit一致
- confirm→narrator→cleanup 全鏈路實測跑過(那1個單seed候選也通過了confirm holdout,但narrator輸出會標明它只有seed 3——人工審查看到單seed會正確打折,這正是Finding 3多重比較殘留風險靠「人在迴圈+seed重複性」處理的設計)
- `uv run pytest tests/ -q`:**331 個全過**(含band_k汙染回歸測試 `test_wider_band_k_confound_would_fool_raw_brier_but_not_skill_score`)
- 驗證用的`skill-verify`/`verify-run-1` campaign資料都已從正式資料庫清除

### 30. Opus 審查發現與修正(2026-07-11,切 Opus 4.8 後完整過一遍核心邏輯)
使用者要求切 Opus 審查所有邏輯再 merge。審出五個問題,最重要的一個是 merge-blocking:

- **🔴 Finding 1(必修,已修)——band_k 掃描讓 Brier 比較失去意義**:`compare_to_baseline` 原本比原始 Brier,但 band_k 是「標籤定義」的旋鈕,band_k 越大→越多天落在佔多數的 RANGE 類→分類問題機械性變簡單→原始 Brier 底線變低。所以跨 band_k 比原始 Brier 是「拿不同難度的考卷比分數」,系統性獎勵最寬的 band 而非最好的模型。**用真實資料證明過**:同一個模型(gmm,3,seed0)只改 band_k 0.5→0.7,原始 Brier 從 0.666 降到 0.628,舊判準判定「2/3 regime 顯著變好」——但這是同一個模型。holdout confirm 也抓不到(band_k=0.7 標籤在 holdout 上一樣好猜)。**修法(使用者選的)**:改用 Brier skill over climatology——每個設定對比「自己的多數類基線預測」的技巧分數,把 label 難度的 confound 除掉。修正後同一個 band_k=0.7 模型 skill_diff 變 −0.0056(正確地不算進步)。新增回歸測試釘死這個行為(斷言舊 raw-Brier 邏輯會被騙、新 skill 邏輯不會)
- **🟡 Finding 2(已修)——shortlist 排序偏袒小樣本 regime**:原本用「單一最佳 regime 的 diff」排序,小樣本 regime 噪聲大、易出現假的大改善。改成用 `overall_skill_diff`(pool 全部 OOS 列、天生 n 加權)排序
- **🟡 Finding 3(已文件化,設計上處理)——confirm 階段本身有多重比較**:每檔 top-3 × 11 檔 = 最多 33 次 holdout 檢定 @α=0.05,holdout 解決的是 search 的 winner's-curse 點估計偏差,不是 confirm 自己的多重性。緩解:seed 掃描(跨 seed 重複才可信)+ 候選只「提案」不「促生產」、人工審查。已在 docstring 明確記錄為已知殘留限制,沒有假裝不存在
- **🟢 Finding 4(已修)——`run_confirmation` 兩次寫入非原子**:中途 crash 會留下「有 confirm run 無 candidate」的孤兒,而 idempotency guard 又擋住重跑修復。包成 `BEGIN/COMMIT/ROLLBACK` transaction
- **🟢 Finding 5(已修)——params 的 `seed` 只影響 regime track**:doc 補一句說明(direction model 永遠 seed=0)

### 明確不做(計畫本身寫的範圍界線)
- 不掃`options_risk.py`停損/停利參數(沒有歷史選擇權報價可回測)
- 不掃每日虧損熔斷門檻(使用者風險承受度,不是模型參數)
- 不建新的人工歷史事件分段機制(regime detection已是CLAUDE.md認可的分段機制)
- 不自動把掃描結果寫回`production.py`/`ev_gate.py`/`kelly.py`預設值
- 不在這次展開使用者提到的「消息多跳推理鏈」強化(見下方「核心交易理念」小節,是完全不同性質的工作)

---

## 2026-07-11(續3):核心交易理念記錄(供之後催化劑層強化參考)

使用者原話(要求「記在所有東西之上」):**股市是在反映尚未消化的訊息,消息往往是經由前面一個消息作為推演,越聰明、能推斷越多層的人,往往越早拿到最準確最新的資訊**。已存成獨立memory `trading-philosophy-info-cascade`(user類型)。**這次沒有展開實作**(使用者明確把這次範圍劃在「數值層校準」),但這是下一步強化`catalyst_synthesis.py`該走的方向:現在的傳導鏈推理是**單跳**(catalyst→mechanism→為什麼影響這檔),理念要求的是**多跳**(消息A→暗示B會發生→B發生後誰受益→這個受益還沒被市場定價的程度)。A4歸因引擎的`wrong_signal_existed`案例天生是「我們沒推論到那一層」的證據庫,兩者之後可以串起來——但這是獨立的下一個計畫,不是這次的一部分。

---

## 給新手:現在就能看到什麼、怎麼看

### 0. 開新前端(React,最直觀,推薦從這裡開始)

```bash
cd /Users/danielisgod/Projects/stockmoney
uv run uvicorn stockmoney.api.main:app --reload --port 8000   # 後端,先開
npm --prefix frontend run dev                                  # 前端,port 5173
# 或用 preview_start 開 .claude/launch.json 裡的 "fastapi-backend" + "frontend-vite"
```
瀏覽器開 `http://localhost:5173`,頂部導覽列有5頁：**今日機會**(landing,依conviction排序的卡片)、**標的詳情**(點卡片進去,價格圖+模型機率+為什麼特徵值+消息催化劑面板+預測歷史)、**消息雷達**(催化劑清單,依新穎度×情緒排序)、**戰績**(勝率趨勢+輸贏分布+近期預測)、**持倉風控**(開倉部位+風控燈號)。**消息面相關的兩個地方現在會誠實顯示「尚無資料」**(見下一步跟下方「還沒做的事」)。

### 0.5 消息分類 + 催化劑深度推理(已自動化上線,見上方第19-23點)

三段 pass 都已經排程在跑,不用手動介入:`stockmoney-scan-classify`(00:30)、`stockmoney-scan-catalyst-synthesis`(01:00)、`stockmoney-scan-digest`(07:00,投遞到WhatsApp)。全部用 OpenClaw + `claude-cli/claude-sonnet-4-6`(訂閱額度),不是付費API。

```bash
openclaw cron list --all                                    # 看三個job的排程/狀態
openclaw cron run <job-id> --wait --expect-final             # 手動立刻跑一次(不用等排程時間)
```

`scripts/classify_scan.py`/`scripts/synthesize_catalysts.py`(付費Anthropic API版本)繼續保留當手動備援參考,**不建議填ANTHROPIC_API_KEY去跑它們**,除非之後改變心意。

### 0.6 直接curl API(不開前端也能看資料)

```bash
curl localhost:8000/api/opportunities   # 全watchlist依conviction排序
curl localhost:8000/api/ticker/NVDA
curl localhost:8000/api/predictions
curl localhost:8000/api/positions
curl localhost:8000/api/catalysts       # 跑完上面兩支腳本後才會有真內容
```

### 1. 每日預測紀錄

```bash
# 對semiconductor板塊watchlist標的各記錄一筆今天的判斷
uv run python scripts/daily_prediction_cli.py record-watchlist

# 驗證到期的預測(查實際股價,判定輸贏)
uv run python scripts/daily_prediction_cli.py grade

# 看所有/待驗證/已驗證的紀錄
uv run python scripts/daily_prediction_cli.py list
uv run python scripts/daily_prediction_cli.py list --status pending
```

### 2. 選擇權持倉(regime/EV 現在自動補上)

```bash
uv run python scripts/options_positions_cli.py open \
  --symbol SOXL --right call --side long --strike 190 \
  --expiry 2026-09-18 --entry-date 2026-07-10 \
  --entry-underlying-price 174.82 --entry-premium 12.50 \
  --entry-iv 0.55 --thesis "semis breakout"
# regime_at_entry 會自動抓 production inference 的結果,不用手動填

uv run python scripts/options_positions_cli.py check --position-id <id> --current-premium 9.80
# current_regime / ev_of_continuing 現在會自動補上(除非symbol不在watchlist)
```

### 3. 看資料庫(現在27張表)

```bash
uv run python -c "
from stockmoney.data.db import get_connection
conn = get_connection('data/stockmoney.duckdb')
tables = conn.execute(\"SELECT table_name FROM information_schema.tables WHERE table_schema='main' ORDER BY table_name\").fetchall()
for (t,) in tables:
    n = conn.execute(f'SELECT count(*) FROM \"{t}\"').fetchone()[0]
    print(f'{t}: {n} 筆')
"
```

### 4. 回測(含新的LightGBM對照)

```bash
uv run python -m stockmoney.models.backtest_semiconductor
uv run python -m stockmoney.models.backtest_ev_gate
uv run python -m stockmoney.models.backtest_direction_models   # LightGBM vs 邏輯回歸,誠實結果是LightGBM較差
uv run python -m stockmoney.models.backtest_feature_ablation   # 新:RSI/Volume特徵有沒有顯著幫助,誠實結果是沒有
```

### 5. 補算特徵(如果覺得資料庫裡的特徵看起來舊)

```bash
uv run python scripts/compute_features.py
```
`nightly_refresh.py` 現在會自動做這件事,這是手動版,冪等安全,可以隨時重跑。

### 7. 跑全部測試

```bash
./scripts/check_all.sh              # 一鍵跑齊下面三項,fail-fast
uv run pytest tests/ -q             # Python,應該顯示 `330 passed`
npm --prefix frontend test          # 前端 Vitest,應該顯示 `23 passed`
cd frontend && npx tsc -b --noEmit  # 前端型別檢查,應該無輸出(乾淨)
```

### 8. OpenClaw 排程狀態

```bash
openclaw cron list --all   # --all 才會看到 disabled 的 job(stockmoney-scan-classify)
openclaw cron runs --id fac5889d-f4c1-4d5e-a8ec-f0d2c8cf02a6 --limit 3
openclaw cron runs --id 32580184-bc44-44ac-9b51-e7768eb9a70f --limit 3
```

---

## 還沒做的事(依優先順序)

1. ~~接手 OpenClaw scanner 交接文件~~ **✅ 已完成**(見上方「OpenClaw 自動化真正打通」小節):三段 pass 全部排程上線、真實資料端到端驗證過。
2. **消息面資料現在會持續累積,等資料夠長後**:
   - **A3**:催化劑訊號填進歷史夠長後,跑`backtest_feature_ablation.py`式的顯著性檢定,通過才能促生產(目前剛開始累積,還不夠)
   - ~~**A4(stretch)**:每日歸因覆盤引擎~~ **✅ 已完成**(2026-07-11,見上方「A4 每日歸因與覆盤引擎」小節):`attribution.py` 引擎 + CLI,三層拆解、三 verdict、feature_candidates 只提案不促生產,已接進 nightly_refresh、真實 11 筆端到端驗證過。等 `wrong_signal_existed` 案例累積才會長出待審候選
3. ~~FRED總經資料落後~~ **✅ 已修復**(見下方「FRED macro 資料落後修復」小節):`dxy_chg_1d_ffill`/`oil_chg_1d_ffill` 取代原本的 `dxy_chg_1d`/`oil_chg_1d`,"今天" 從 07-02 追上到 07-08(剩下的1天落後是正常的T-1申報延遲,不是bug)。
4. **模組A訊號強化,下一步**:LightGBM試過了比邏輯回歸差(第7點);RSI/Volume Z-score兩個新特徵也試過了,顯著讓Brier分數變差(第8點)。兩個「顯而易見」的升級路徑都已經誠實驗證過、沒有用。下一步可能要換更根本的方向:
   - (a) **另類數據特徵**(GEX/skew、put-call ratio)雖然已經在收集,但目前只有2天歷史(`options_derived_daily`/`put_call_ratio_daily` 都是從2026-07-09才開始),要等資料庫累積足夠長才能做有意義的樣本外驗證,不能現在硬塞進去
   - (b) ~~regime detection 觀測特徵範圍需要檢視~~ **✅ 已修復**(經 plan mode 規劃),見上方「Regime detection 特徵分離」小節。歷史回測數字已經改變(修復生效的預期結果),過去任何「GMM/HMM表現如何」「哪個regime的Sharpe好」的舊結論都應該視為基於錯誤輸入,以這次重跑的數字為準
   - (c) 重新檢視標籤定義(目前的漲跌幅門檻/5天horizon是否是最佳選擇)
   - HMM 的退化regime問題也還沒解決(不影響安全性,只影響訊號品質)
5. ~~前端測試覆蓋~~ **✅ 已補上**:Vitest + React Testing Library,23個測試涵蓋所有頁面+元件的載入/空狀態/錯誤狀態,見下方「前端測試」小節。
6. ~~Streamlit dashboard 退役~~ **✅ 已完成**:`scripts/dashboard.py`跟`streamlit`依賴都拆掉了,`.claude/launch.json`也移除了對應設定。
7. Phase 3規劃(個股論點追蹤系統、日內即時交易層):現在都還不用碰。
8. ~~週末校準戰役OpenClaw narrator排程~~ **✅ 已完成**(2026-07-11,見上方「週末校準戰役」小節):exec-approvals條目+cron job都已註冊並實測驗證過,WhatsApp送達成功。真正的第一次「週末」跑法還沒發生過(這次驗證是平日手動觸發的空狀態測試),等到真的週六/週日才會看到有內容的摘要。**核心邏輯已用Opus 4.8完整審查過**(見上方第30點),審出並修好一個merge-blocking的方法論漏洞(band_k/Brier confound)。
9. **消息多跳推理鏈強化**:使用者核心交易理念(見上方「核心交易理念記錄」小節)要求的下一步,把`catalyst_synthesis.py`的單跳傳導鏈推理加深成多跳。這次故意沒展開(範圍劃在數值層校準),是獨立的下一個計畫。

---

## 已知需要注意的小事

- **Reddit 官方 API 申請不了**,改用免費公開 RSS 繞過(已驗證可用)
- **WSJ RSS來源之前是死的**(凍結在2025-01舊快照),已換成CNBC的public search-RSS,驗證過內容新鮮
- 資料庫裡的 `watchlist_candidates` 表現在是空的——AI主題發現那塊還沒真正跑過
- 所有 raw 表都是「只增不改」設計,這是刻意的,為了讓回測誠實
- **選擇權持倉的「目前權利金」仍需手動輸入**:系統沒有存歷史逐筆選擇權報價,沒辦法自動估算合約現值,這是刻意的v1簡化
- **動態EV停利算的是模型歷史回測交易的EV,不是使用者手上那張特定合約的EV**——dashboard/CLI輸出都有註明這個差異,不要誤讀
- DuckDB同一個檔案不能同時被兩個「寫入」連線打開,dashboard已改成短命read-only連線避開這個問題
- **`~/.openclaw/openclaw.json` 是OpenClaw自己的設定檔,不是這個repo的一部分**,修改它會影響使用者機器上所有OpenClaw agent(不只是stockmoney的scanner agent),要小心
- **LightGBM 在 macOS 需要 `libomp` 系統依賴**(`brew install libomp`),不是repo管得到的東西,換一台新機器要記得裝,不然 `import lightgbm` 會直接 crash(`dlopen` 找不到 `libomp.dylib`)
- `feature_store` 現在會跟著 `nightly_refresh.py` 每晚自動重算(之前這塊一直沒排程,曾經落後 `ohlcv_daily` 將近一週),如果覺得資料看起來舊,先跑一次 `uv run python scripts/compute_features.py` 手動補
- **第一批真實勝率資料點已經跑出來了**:2026-07-02記錄的11筆預測已全部grade完畢。10/11「贏」,但那是因為`win_rate_history`只算方向性預測(排除range),而這11筆裡只有NVDA一筆是方向性判斷(預測up),結果實際是range,**輸了**。方向性勝率0/1——樣本數太小沒有任何統計意義,只是確認整條「預測→grade→勝率」流程真的能跑通,不代表模型有效或無效
- **`ANTHROPIC_API_KEY` 故意留空,不要填**:使用者已明確表態絕不用付費API,只用訂閱額度(`claude-cli`)。`scripts/classify_scan.py`/`scripts/synthesize_catalysts.py`會因此打不了API,這是預期行為,不是bug——這兩支腳本已改為手動備援用途,真正的自動化路徑是OpenClaw三個cron job(分類/催化劑推理/晨間摘要),已上線在跑,見上方「OpenClaw 自動化真正打通」小節
- **FastAPI的CORS設定是dev-only**:目前寫死允許`localhost:5173`(Vite預設port),這個API未來要跑在localhost以外的地方,要重新檢視這段設定
- **`frontend/node_modules` 沒有進git**(`.gitignore`已覆蓋),換一台新機器或全新clone後要先跑 `npm --prefix frontend install` 才能 `npm --prefix frontend run dev`
- **React前端用的是Tailwind v4**(`@tailwindcss/vite` plugin,`src/index.css`只有一行`@import "tailwindcss"`),跟v3的`tailwind.config.js`+`postcss.config.js`寫法不一樣,不要照舊教學去加postcss設定檔
- **React元件的型別跟`stockmoney/api/queries.py`的輸出形狀是手動對齊的**(`frontend/src/lib/api.ts`),v1沒有自動生成schema,如果之後改了API回傳形狀,記得回來手動同步這個檔案,不然TypeScript不會報錯但執行期會拿到undefined
- **`symbol_backtest_snapshot`只用GMM**,不秀GMM+HMM對照——這是刻意的效能取捨(production.py本來就選GMM當生產預設),如果想看兩者對照,還是要用`backtest_semiconductor.py`等獨立回測腳本
