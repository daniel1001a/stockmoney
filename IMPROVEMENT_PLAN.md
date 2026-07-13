# stockmoney — 進步計畫 & 平行開發藍圖 (2026-07-11)

> 給每個新 session 的第一句話:**「讀 IMPROVEMENT_PLAN.md 的 Session N 那一節,加上 CLAUDE.md,先進 plan mode 確認計畫再動手。」**
> 這份檔案的目的:讓你可以同時開多個 Claude Code session 平行開發,彼此不打架。每一節標明 **負責檔案(ownership)**、**依賴**、**model/effort**、**驗收標準**。

---

## 0. 核心診斷 — 錢還沒進來的真正原因

基礎建設是這個專案的強項(walk-forward 紀律、look-ahead 防護、leakage canary 測試、calibration campaign、league 競賽框架、408 個測試),**這很罕見、不要動它**。問題不在工程,在 **alpha(訊號本身太弱,方向準確率 0.38–0.45,只比瞎猜好一點點)**。下游的 EV 閘門、Kelly、選擇權風控都做得很好 —— 但它們在過濾一個沒有 edge 的訊號,等於精緻地篩選雜訊。

**五個真正的瓶頸(依對獲利的影響排序):**

| # | 瓶頸 | 白話 | 為什麼是它 |
|---|---|---|---|
| **1** | **選擇權/資金流特徵完全沒用上** | migration 003–009 把 IV surface、put/call ratio、VIX term structure、GEX/skew、ETF 資金流全抓進資料庫了,但 `data/features/` 只有價格/總經特徵模組 —— 方向模型的 6 個特徵裡**一個選擇權訊號都沒有** | 對一個**做選擇權的系統**,選擇權市場自己的定位(dealer gamma、skew、term structure、put/call)是最有預測力的短線訊號群,現在完全閒置。這是最大、最便宜的一槍。 |
| **2** | **預測目標對選擇權是錯的** | 模組 A 預測 5 天後「漲/盤/跌」(用波動帶門檻)。但選擇權賺賠靠的是**幅度 + 路徑 + IV 變化**,不是純方向。方向對了、long option 照樣被 theta + IV crush 吃掉 | 選擇權最穩定的 edge 通常是 **波動率風險溢價(RV vs IV,IV 是貴還是便宜)** 跟 **skew**,不是猜方向。現在的單一方向分類器在優化錯的目標函數。 |
| **3** | **league 競賽根本沒在交易選擇權** | 你明確要求選擇權加入競賽。現在 trader 只吐 up/down/range,用方向命中率評分。`option_selection.py` + `options_pnl.py` 的定價/回測管線都在,只是**沒接進 league** | 一接上,競賽就從「猜方向比賽」變成「選擇權損益比賽」,直接對齊你真正在做的事。 |
| **4** | **只有 2 個 trader** | Chartist(模組A)+ Analyst(催化劑)。整整少了一票流派:賣波動、skew/期限結構、動能趨勢、均值回歸、事件波動、gamma/flow、跨股 dispersion | 你要的「把所有策略灌進超級大腦」——league 框架已經能同時跑、評分、meta 分配,只差把策略當 engine 寫進去。 |
| **5** | **持續改進迴圈還沒閉環** | `review.py` 會產出「方法更新提案」但人工把關(這是對的)。缺的是:排程每晚自動跑 league → grade → review → 提案 →(人核准)→ 冠軍/挑戰者自動晉升 | 你要的「不間斷訓練 + 分析輸贏原因 + 各自進步」。90% 建好了,差把迴圈接起來 + 排程 + champion/challenger 機制。 |

**次要但重要:** 方向模型 per-regime 只在幾百筆樣本上訓練 → 過擬合陷阱(這也是 LightGBM 輸給邏輯回歸的原因)。解法是**跨標的 pooling**(一個模型跨所有 watchlist 訓練,用 symbol/sector 當特徵),樣本數瞬間 ×10。這是 Session 6。

---

## 1. 七個 Session — 平行開發拆分

**依賴/衝突分析(決定哪些能同時開):**
- `feature_matrix.FEATURE_COLUMNS` 會被 S1 / S2 / S6 動到 → 不能同時改,S1 先行。
- `league/engines.py` 會被 S3 / S4 動到 → S3 先重構 grading,S4 再加 engine。
- 完全獨立、隨時可平行:**S1(特徵層)**、**S7(crypto,獨立 namespace)**。

**建議三波:**
- **第一波(現在就能三個平行開):** S1 特徵、S3 選擇權進競賽、S7 crypto 連接器
- **第二波(等第一波):** S2 波動率目標(需要 S1 特徵)、S4 新 trader(需要 S3 重構)、S5 閉環迴圈
- **第三波:** S6 pooled 模型、crypto 回測框架

| Session | 主題 | Model / Effort | Ownership(主要檔案) | 依賴 |
|---|---|---|---|---|
| **S1** | 選擇權/資金流特徵 | **Sonnet 5 / high**(先 plan mode 確認 available_at 防洩漏) | `data/features/options_micro.py`(新)、`fund_flow.py`(新)、`feature_matrix.py`、`backtest_feature_ablation.py`、`scripts/compute_features.py` | 無 |
| **S2** | RV-vs-IV 波動率目標 + 新預測頭 | **opusplan**(Opus 4.8 規劃 xhigh + Sonnet 執行) | `models/vol_forecast.py`(新)、`models/vrp_gate.py`(新) | S1 |
| **S3** | 選擇權進 league 競賽 | **Sonnet 5 / high** | `league/orchestration.py`、`league/grading_options.py`(新)、`league/league_table.py`、`data/trader_predictions.py` | 無 |
| **S4** | 擴充 trader 陣容(超級大腦) | **Sonnet 5 / medium-high** | `league/engines.py`(append)、`data/migrations/03X_more_traders.sql`(新) | S3 |
| **S5** | 閉環持續改進 + 冠軍/挑戰者 | **Sonnet 5 / high** | `league/review.py`、`league/promotion.py`(新)、`scripts/nightly_refresh.py` | S3, S4 |
| **S6** | 跨標的 pooled 模型 + 機率校準 | **opusplan**(規劃 xhigh) | `models/direction.py`(新 class)、`models/calibration.py`(新)、`models/walk_forward.py` | S1 |
| **S7** | Crypto 市場拓展 | **Sonnet 5 / high**(連接器)→ **opusplan**(crypto 回測設計) | `data/ingestion/crypto_*.py`(新)、`data/migrations/04X_crypto_*.sql`(新)、`models/crypto/`(新) | 無 |

> **省 token 訣竅**:每個 session 只讀自己那一節 + CLAUDE.md 對應章節,不要整份 HANDOFF.md 讀進去(75KB)。要看歷史再讀。

---

## 2. 各 Session 詳細任務 + 驗收標準

### S1 — 選擇權/資金流特徵(最大一槍,先做這個)
**做什麼:** 把已經在資料庫裡、但沒被模型用到的選擇權市場訊號變成特徵。候選(全部要有明確 `available_at` 時間戳,防 look-ahead):
- `put_call_ratio_z`(put/call 比的 20 日 z-score)— 極端值 = 情緒反轉
- `iv_rank_252` / `iv_percentile`(當前 ATM IV 在過去一年的分位)— IV 貴/便宜
- `skew_25delta_chg`(25-delta risk reversal 變化率)— 尾部風險定價變化
- `vix_term_slope`(VIX 期限結構斜率,近月/次月)— backwardation = 恐慌
- `gex_sign` / `gex_z`(dealer gamma 估計正負與強度)— 決定盤面是「被壓平」還是「追漲殺跌」
- `etf_flow_z`(相關 ETF 淨流入 z-score)
**紀律:** 每個特徵都要過 `backtest_feature_ablation.py` 的配對 bootstrap 顯著性檢定(CLAUDE.md §12)。**只有 OOS Brier 顯著變好的才進 `FEATURE_COLUMNS`**,其餘留在 feature_store 待日後重測(跟 rsi/volume 一樣的規矩)。
**為什麼可能成功(跟 rsi/volume 不同):** rsi/volume 跟現有價格特徵高度共線(所以沒用);選擇權/資金流特徵是**正交的新資訊維度**,不是價格的重新包裝。
**驗收:** 至少跑完全部候選的 ablation,把顯著為正的加入生產,附 bootstrap CI 表;`./scripts/check_all.sh` 全過。**誠實回報**:就算全部沒過,也是有價值的結果,照 HANDOFF 慣例記錄。

### S2 — RV-vs-IV 波動率目標(重新定義要預測什麼)
**做什麼:** 新增第二個預測頭,不猜方向,猜**未來已實現波動 vs 當前隱含波動**(波動率風險溢價 VRP)。
- Label:`forward_realized_vol_Nd − entry_implied_vol`(entry 當下可得的 IV)。正 = 買方有利(股票會動得比 IV 定價的多),負 = 賣方有利。
- 新 `vrp_gate.py`:類比 `ev_gate`,但門檻是 VRP 分佈的滾動分位。VRP 顯著為正 → 傾向買 straddle/directional long;顯著為負 → 傾向賣 premium(sell put / credit spread)。
**Look-ahead 高風險點(規劃階段 opus xhigh 必須把關):** label 用到 forward realized vol,entry IV 必須是 entry 當日的值,walk-forward 切分要跟現有 `feature_matrix` 的 resolved/unresolved 邏輯一致。務必加對應的 leakage canary 測試。
**驗收:** VRP 預測的 OOS 校準(reliability curve)+ 一個「用 VRP gate 決定買方/賣方」的 options-P&L 回測,對照「永遠買方」baseline。

### S3 — 選擇權進 league 競賽
**做什麼:** 讓每個 trader 的 call 不只是方向,而是一個具體選擇權結構,用真實選擇權損益評分。
- 每個 `EngineCall` 增加 optional `option_structure`(用現有 `option_selection.select_option` 把 direction+conviction 映射成 30-DTE、target-delta 的 call/put;賣方策略映射成 credit spread)。
- 新 `grading_options.py`:到期用 `options_pnl.py` 定價算真實選擇權損益,寫進 `trader_predictions`(新增欄位 `option_pnl`)。
- `league_table.py` 增加 `option_pnl` / `option_sharpe` / `option_win_rate` 欄位,跟現有方向命中率並列(兩個都看:方向對不對 vs 選擇權有沒有賺)。
**驗收:** 跑一次 league,兩個 trader 都有選擇權損益數字;新增測試涵蓋「方向對但選擇權賠錢」(theta/IV crush)這個關鍵 case。

### S4 — 擴充 trader 陣容(超級大腦,見 §3 策略清單)
**做什麼:** 在 `engines.py` 加 4–6 個新 engine,每個是一個交易流派,全部唯讀現有資料、**不打付費 API**:
1. `VolSellerEngine` — IV rank 高 + regime 盤整 → 賣 premium(靠 S1 的 iv_rank 特徵)
2. `SkewEngine` — 25-delta skew 極端 → risk reversal 方向
3. `TrendEngine` — ADX 高 + 動能 → 順勢 long option
4. `MeanRevEngine` — put/call z 極端 + 短線超買超賣 → 反轉
5. `EventVolEngine` — 財報/Fed 前 → 買 straddle(靠 event_calendar 表)
6. `DispersionEngine` — 跨股 dispersion 飆升 → 個股 vs ETF 相對倉位(CLAUDE.md §8 的 AVGO 案例)
每個 engine 都在 `032_trader_methods.sql` 風格下 pin 一個 method version。
**驗收:** 全部 engine 註冊進 `ENGINE_REGISTRY`,league 一晚同時跑 8 個 trader,league table 排名出來;每個 engine 有 skip 邏輯的單元測試。

### S5 — 閉環持續改進 + 冠軍/挑戰者
**做什麼:** 把「跑→評分→檢討→提案→晉升」接成不間斷迴圈。
- `promotion.py`:champion/challenger 機制。每個策略族有一個「現役」method version 和若干「挑戰者」。挑戰者在**獨立 holdout** 上連續 N 個 grading 週期贏過現役(且 bootstrap CI 排除 0)才提案晉升 —— 人工核准才真的換(CLAUDE.md §7 禁止自動回寫)。
- 把 `run_predictions → grade → run_review → league_table → promotion 提案` 串成一個 `scripts/nightly_league.py`,接進 `nightly_refresh.py`。
- **meta-allocator**:league_table 的排名決定 dashboard 上「今天聽誰的 + 部位加權」,輸的 trader 自動降權(但不淘汰,留著看 regime 變了會不會翻身)。
**Look-ahead / 回饋污染高風險點:** 人工 override 與晉升決策必須走 Meta Layer 獨立紀錄,絕不回寫主模型訓練資料(CLAUDE.md §7)。
**驗收:** 端到端跑一次 nightly_league,產出 league table + 至少一個晉升提案(用歷史資料);提案是 `proposed` 狀態、沒自動生效。

### S6 — 跨標的 pooled 模型 + 機率校準
**做什麼:**
- 新 `PooledDirectionModel`:一個模型跨全部 watchlist 訓練,symbol/sector 當 categorical 特徵(逃離 per-regime 幾百筆的過擬合)。跟現有 per-regime 邏輯回歸做 walk-forward OOS 對照,誰贏用誰(跟淘汰 LightGBM 同一種紀律)。
- 新 `calibration.py`:isotonic / Platt 校準層套在方向機率上(Kelly 部位需要校準良好的機率才不會過度下注)。報 reliability curve + Brier 分解成 calibration/refinement。
**驗收:** OOS Brier + reliability curve 對照表;若 pooled 沒贏,誠實保留 per-regime,記錄結果。

### S7 — Crypto 市場拓展(完整 plan 見 §4)

---

## 3. 交易策略「超級大腦」清單(要編碼進 league 的流派)

把這些當成一份策略字典,每個都是一個 engine(S4 起步,之後持續加)。標 ⭐ 的是選擇權系統最該優先做的:

**波動率類(選擇權核心 edge):**
- ⭐ 賣波動率溢價(IV rank 高、盤整 → sell put / iron condor,達 50–80% 最大利潤提前平倉,規避 pin risk)
- ⭐ 買波動率(VRP 為正、事件前 → long straddle / strangle)
- ⭐ Skew / risk reversal(25-delta skew 極端 → 方向性偏斜下注)
- 期限結構(VIX backwardation → 近月賣、遠月買;calendar spread)
- 跨股 dispersion(個股波動 > 指數波動 → 買個股波動、賣指數波動)

**方向類:**
- 動能/趨勢跟隨(ADX + breakout → 順勢 OTM call/put)
- 均值回歸(put/call z 極端 + RSI 超買超賣 → 反轉,short-dated)
- Gamma / dealer flow(GEX 為負 → 追漲殺跌放大;GEX 為正 → 盤面被壓平,賣波動)

**事件/催化劑類:**
- 財報波動(財報前買 straddle、財報後 IV crush 賣波動)
- 消息傳導鏈(現有 Analyst engine,靠 OpenClaw catalyst_signals)
- 總經跨資產(利率/DXY/原油 regime → 板塊 ETF 選擇權)

**風控/組合層(不是 engine,是規則):**
- Fractional Kelly(現有,0.25–0.5×)
- 每日虧損熔斷(CLAUDE.md §16 待你定門檻)
- 波動率分桶風控參數(現有 calibration_campaign)

> 每加一個 engine,league 就多一個「聲音」,meta-allocator 自動決定聽誰的。這就是「超級大腦」的具體長相 —— 不是一個大黑盒,是一堆可解釋的專家 + 一個記分板。

---

## 4. Crypto 市場拓展 — 完整 Plan(S7)

**定位:** 完全獨立的第二個市場軌道,**共用** league / regime / EV / Kelly / 風控框架,但**資料與模型分離**(crypto 24/7、波動結構、微結構跟股票不同,不共用 pipeline —— 呼應 CLAUDE.md 個股 vs 板塊分流的紀律)。

**Phase C0 — 資料層(Sonnet 5 / high):**
- 連接器(全部免費公開 API,不用金鑰):
  - Binance / Coinbase public REST:OHLCV(現貨 + 永續)
  - 永續資金費率(funding rate)、未平倉量(OI)—— 這是 crypto 的「選擇權微結構等價物」,極有訊號
  - (選配)Deribit public:BTC/ETH IV surface、25-delta skew、DVOL(crypto 版 VIX)—— **這是 crypto 選擇權的核心資料**
  - (選配)on-chain:交易所淨流入、穩定幣供給(免費 API 有限,列 Phase C2)
- 新 migrations:`crypto_ohlcv_daily`、`crypto_funding_oi`、`crypto_iv_surface`(對齊股票版 schema,方便共用特徵函式)
- Crypto watchlist:BTC, ETH 核心;SOL, 主流 alt 次要

**Phase C1 — Crypto regime + 方向模型(opusplan / 規劃 xhigh):**
- **關鍵差異(規劃必須處理):** 24/7 沒有收盤 → walk-forward 的「日」定義、label horizon、available_at 都要重想。用 UTC 00:00 當日界或滾動窗口,規劃階段定案。
- Crypto 專屬特徵:funding rate(極端正 = 多頭過熱要反轉)、OI 變化、DVOL term structure、perp-spot basis
- Regime:crypto 只有兩個有意義 regime 也可能夠(趨勢 / 震盪),不要硬套股票的 3 個 —— 用 OOS 表現決定

**Phase C2 — Crypto 選擇權策略(對齊股票軌道):**
- Deribit IV surface → 同一套 VRP / skew engine 套用到 BTC/ETH 選擇權
- Crypto 波動率溢價通常比股票更肥(散戶買方多)→ **賣波動策略在 crypto 可能 edge 更大**,優先驗證
- 資金費率套利提示(不下單,只提示 funding 極端 → 現貨/永續價差機會)

**Phase C3 — 進 league 競賽:** crypto trader 跟股票 trader 同一個 league table,但分市場報告(不合併勝率,CLAUDE.md §12 紀律)。

**風險提醒(寫進 CLAUDE.md 邊界):** crypto 一樣**只提示、不下單、不接交易所 API**。crypto 詐騙/rug 風險高,watchlist 只放高流動性主流幣,不做小幣。

---

## 5. OpenClaw 待辦(另一台機器,這台無法測試)

> 這台電腦**不開發 OpenClaw 功能**,只登記點子 + 給你到另一台機器怎麼建 agent 的指引。每一項都標明「這台先產出什麼,OpenClaw 那台接什麼」。

### 通用建 agent 步驟(每個任務共用)
1. 在這台把要跑的**純 Python 腳本**寫好、測好(手動 `uv run` 能跑對)——OpenClaw 只負責「排程 + 跑指令 + 把結果送出來」,不放邏輯在 agent 裡。
2. 到 OpenClaw 那台,寫一個 skill(`~/.openclaw/skills/<name>/SKILL.md`),裡面**只允許執行你指定的 1–3 個指令**(參考現有 `stockmoney-scanner` skill 的 exec allow-list 寫法)。
3. 用 `claude-cli/claude-sonnet-*`(訂閱額度,**不是**付費 API,呼應 CLAUDE.md §13 硬性要求)。
4. 註冊 cron:`openclaw cron add`,設好時間 + model + skill。
5. 端到端手動觸發一次驗證,再交給排程。
6. **安全:** 只用你自己寫的 skill,不裝 ClawHub 社群技能;金鑰不進 agent context。

### 待辦清單

- [ ] **TODO-OC-1｜每晚 league digest + 冠軍挑戰者通報**
  - 這台產出:`scripts/nightly_league.py`(S5)輸出 league table + 晉升提案 JSON
  - OpenClaw 接:cron 每晚跑該腳本 → Sonnet 把結果寫成人話 → WhatsApp/Discord 送「今天最強的一手 + 目前 league 排名 + 待你核准的晉升提案」
  - 為什麼用 OpenClaw:排程 + 推播 + 自然語言摘要,正是它擅長的

- [ ] **TODO-OC-2｜Crypto 24/7 掃描**(crypto 不睡覺,這台批次跑不動)
  - 這台產出:`scripts/crypto_scan_ingest.py`(S7 的 C0)
  - OpenClaw 接:cron 每 2–4 小時拉 funding/OI/social,寫進 DuckDB;funding 極端時主動推播提示

- [ ] **TODO-OC-3｜財報/事件波動預告**
  - 這台產出:讀 `event_calendar` 表、標出 3 天內有財報的 watchlist 標的的查詢腳本
  - OpenClaw 接:每晚跑 → 推播「明後天有財報,event-vol engine 建議關注 X/Y」

- [ ] **TODO-OC-4｜方法更新提案的深度論證**(已有基礎可延伸)
  - 這台產出:S5 的 review 提案(`proposed` 狀態)
  - OpenClaw 接:Sonnet 讀提案 + 相關 catalyst/attribution,寫一份「支持/反對這次晉升」的人話報告,附在推播裡給你決策

- [ ] **TODO-OC-5｜非結構化選擇權 flow 情緒**(延伸現有 classify)
  - 這台產出:擴充 `scan_classify` 抓「異常選擇權活動」提及(unusual options activity 貼文)
  - OpenClaw 接:現有 `stockmoney-scan-classify` cron 加這個分類維度

---

## 6. 建議動手順序(給你不迷路)

1. **今天就開三個 session 平行:** S1(特徵)、S3(選擇權進競賽)、S7-C0(crypto 連接器)
2. S1 一有顯著特徵進生產 → 開 S2(波動率目標)、S6(pooled 模型)
3. S3 完成 → 開 S4(新 trader),接著 S5(閉環)
4. 全部穩定後把 OpenClaw 待辦搬到另一台機器
5. **每個 session 都先 plan mode**,S2/S6/S7-C1 這三個 look-ahead 高風險的,規劃階段親自把關(CLAUDE.md §15)

**不變的鐵律(每個 session 都遵守):** 只提示不下單、walk-forward 樣本外驗證、防 look-ahead、誠實回報「試了沒用」、新特徵要過顯著性檢定、不打付費 API(用 OpenClaw 訂閱額度)。
