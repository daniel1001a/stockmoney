# 量化市場情緒/風向預測系統 — 技術規格文件

## 0. 專案定位與邊界(必讀,優先於一切實作細節)

**這是什麼**:一套多源異質資訊融合的短中期市場情緒/風向預測引擎,產出每日觀察清單 + 決策提示 dashboard,供人工在選擇權與個股交易時參考。

**這不是什麼**:
- **不執行任何訂單**。系統對交易的唯一輸出是「提示」(例如「此股已達到停損提示價位」),永遠不連接券商 API 做下單、改單、撤單。這是硬性邊界,不因任何後續需求變更。
- 不是全自動黑盒策略。裁量權永遠保留在人身上。

**資金規模**:10k-50k USD。不為想像中的未來擴容犧牲現階段的可解釋性與嚴謹度。

**交易標的範圍界定**:
- **選擇權**:僅限短-中期(對應核心風向判斷週期 1-9 個月),不做長天期選擇權部位。
- **個股**:可做中長期配置型持有,邏輯與選擇權完全分離,見第 6 節。

**開發優先順序**(不可倒置):
1. **Phase 1(現在)**:模組 A(方向擇時)+ 模組 B(EV交易閘門)+ 選擇權風控軌道
2. **Phase 2(之後)**:模組 C(個股「下一個 PLTR」發現引擎的完整版)、個股論點追蹤系統的深度版本、日內即時交易層(intraday streaming)

---

## 1. 系統架構總覽(七層)

```
外部資料來源層(市場數據/總經/另類數據/事件新聞)
        ↓
特徵工程層(Feature Store,統一 schema、時間戳精確、防 look-ahead)
        ↓
模型層 ─┬─ 模組 A:方向擇時模型(內含 regime detection 雙軌驗證)
        └─ 模組 B:EV 交易閘門(動態校準)
        ↓(訊號衝突 = 有價值訊息,用跨資產/跨股離散度指標融合,不用人工規則仲裁)
裁量層(三層分離架構,見第 7 節)
        ↓
輸出層:每日觀察清單 Dashboard + 風控提示燈號

(側支,只讀,不進訓練迴圈)
每日歸因與覆盤引擎 → 產出「新特徵候選清單」,人工審核後才進特徵工程層
```

**核心設計紀律**:每一層都要能獨立驗證、獨立回測、獨立除錯。禁止端到端黑盒模型——這會讓「訊號衝突歸因」(板塊三的 AVGO vs 半導體板塊案例)完全無法拆解。

---

## 2. 資料層:Schema 與來源

| 維度 | 關鍵欄位 | 時間戳粒度 | 頻率 | 來源 |
|---|---|---|---|---|
| 市場結構 | OHLCV, IV surface(25/50-delta), put/call ratio, VIX term structure | tick→日 | 日終batch | yfinance(v1免費)/ Polygon.io(付費升級選項) |
| 選擇權微結構 | GEX(dealer gamma exposure 近似值)、25-delta skew 變化率 | 日 | 日終 | v1:用 open interest + IV 自行估算;v2 視驗證結果決定是否訂閱 ORATS/CBOE DataShop |
| 總體經濟 | 利率曲線、DXY、原物料、跨資產相關性矩陣 | 日 | 日終 | FRED API(免費) |
| 資金流 | ETF淨流入流出、期貨未平倉量 | 日/週 | 日終/週更 | ETF.com / CFTC COT報告(免費) |
| 另類數據-社群 | 論壇貼文量、情緒分數、熱度一階/二階導數(加速度) | 小時→日 | 每小時爬取,日終聚合 | Reddit API(PRAW),透過 Claude Code cloud routine 排程 |
| 另類數據-開發者 | GitHub star/commit速度 | 日 | 日終 | GitHub API |
| 另類數據-搜尋 | Google Trends指數 | 日 | 日終 | pytrends |
| 事件-日曆 | 財報日曆、Fed會議、CPI/NFP時間 | 事件 | 排程 | 財經日曆API |
| 事件-新聞 | GDELT事件分類、地緣座標、語氣評分 | 15分鐘 | 批次拉取 | GDELT(免費) |
| 衍生特徵 | 跨股離散度指標(idiosyncratic/systematic decomposition)、cross-sectional ranking | 日 | 日終計算 | 內部計算 |

**⚠️ 資料完整性鐵則**:每筆爬取資料必須記錄「實際可得時間戳」(不是事件發生時間,是系統真正抓到這筆資料的時間),否則無法誠實回測,直接違反板塊一的 look-ahead bias 防範原則。

---

## 3. Watchlist 設計(混合式,可動態調整)

**v1 核心清單**(深度特徵工程全覆蓋):
- 半導體:NVDA, AVGO, AMD, TSM, SOXL, SOXS
- 大型科技:AAPL, MSFT, GOOGL, META, AMZN

**次要觀察名單**(輕量指標監測):能源、銀行股等,待跨板塊回測驗證訊噪比後決定是否升級。

**動態擴充機制**:輕量掃描器每週依成交量、選擇權未平倉量變化、板塊分類等條件建議候選標的,人工保留最終否決權。

---

## 4. 模型層:Regime Detection(雙軌驗證,核心中的核心)

**不預設 KMeans/GMM 或 HMM 誰更好,兩者都建,用樣本外表現決定。**

- **觀測特徵**:已實現波動率、趨勢強度(如 ADX)、跨股離散度指標
- **狀態數**:先固定 3 個 regime(趨勢多頭 / 趨勢空頭 / 震盪盤整),不用資訊準則(BIC/AIC)搜索最優狀態數(小樣本下容易過擬合)
- **驗證機制**:兩套 regime 分類器各自驅動一套完整下游方向模型,用 walk-forward 樣本外 Sharpe / Brier score 決定採用哪一套,或加權混合
- **v1→v2 路徑**:先確保基礎架構跑通,兩軌驗證框架建好後,誰的樣本外表現好就用誰,不預先偏好複雜模型

---

## 5. 模型層:方向擇時模型(模組 A)

- **v1 演算法**:LightGBM(梯度提升樹)+ 邏輯回歸 baseline 對照組。表格型特徵下優於神經網路,訓練快、可解釋(feature importance 直接支援覆盤引擎)
- **輸出**:機率分佈 P(上漲)/P(下跌)/P(盤整),每個 regime 各自訓練獨立參數
- **個股 vs 板塊分流**(呼應 AVGO 案例):
  - 總經層模型:判斷板塊/大盤方向,驅動 ETF(如 SOXL)選擇權決策
  - 個股層模型:判斷個股是否會脫離板塊 beta(用 idiosyncratic return 拆解),驅動個股獨立倉位決策
  - 兩者特徵集與訓練資料分離,不共用同一套 pipeline

---

## 6. 模型層:EV 交易閘門(模組 B)

```
EV = P(win) × avg_win_size - P(loss) × avg_loss_size - transaction_cost
```

**動態校準(v1:滾動百分位數)**:
- 維護過去 90 個交易日(可調)的 EV 分佈
- 僅當今日 EV 落在該滾動窗口前 25% 分位數以上才視為「可交易」
- 窗口長度與分位數門檻皆為待優化超參數,用歷史回測反推

**升級路徑**:v1穩定後 → v2 接入 regime-conditional 校準(不同 regime 各自維護 EV 分佈)→ v3(可選)貝氏線上更新

---

## 7. 裁量層:三層分離架構(人為輸入介面)

**核心原則**:人為輸入只影響「這一筆交易的部位大小」,絕不回寫模型或訓練資料,避免回饋迴圈污染。

1. **Model Layer**:純資料驅動輸出機率分佈,不接受人為修改,永遠保持乾淨可回測
2. **Discretion Layer**:人的信心調整只作用在 Kelly 部位大小的乘數(見第 9 節),不影響模型輸出本身
3. **Meta Layer**:所有人為 override 單獨記錄成獨立時間序列,每季拿出來做行為分析(「不信任模型時,是模型真的錯還是人在恐慌」),完全隔離於主模型資料流之外

---

## 8. 訊號衝突處理:跨股離散度指標

不用人工規則判斷「有沒有消息」。用因子模型拆解:

```
R_stock = β × R_market + R_idiosyncratic
```

當同板塊個股報酬率標準差(cross-sectional dispersion)突然飆升 → 代表市場正在用消息篩選贏家輸家,而非跟隨大盤情緒。此指標作為 regime detection 的輸入特徵之一,自動反映「有無消息主導」的市場狀態,不需人工定義規則。

---

## 9. 部位大小:Fractional Kelly

```
Kelly% = (P(win) × avg_win/avg_loss − P(loss)) / (avg_win/avg_loss)
實際部位 = Kelly% × 0.25~0.5(保守係數,待回測校準) × 裁量層信心調整倍數
```

保守係數必要,因為模型機率估計必有誤差,全額 Kelly 在估計誤差下會導致過度下注。

---

## 10. 風控機制(僅提示,不執行訂單)

### 10.1 選擇權軌道(短-中期)

**參數不採業界慣例定值,採「波動率分桶 + walk-forward 網格搜索」**:

1. Watchlist 標的依歷史已實現波動率分桶(低/中/高波動)
2. 桶內標的共享同一組風控參數,對 Z值停損門檻 × 停利倍數 × regime條件做網格搜索
3. 目標函數:**風險調整後勝率/期望報酬**(非原始勝率,避免用超緊停利製造虛假高勝率)
4. 貝氏收縮:樣本數少的桶,參數向業界慣例(v1起點值,見下)收斂;樣本數多、統計顯著的桶,允許更大偏離

**v1 起點值(未優化前的預設,待資料驗證後校準)**:
- 價格停損:進場價反向 -2 個隱含波動率標準差(Z值框架,呼應板塊五)
- 權利金停損:買入成本 -50%
- 固定停利:買入成本 +100%
- 動態EV停利:當下續抱期望值 < 現在平倉期望值時提示(用模組B機制反向使用)
- 邏輯失效停損:持倉依賴的 regime 判斷在持倉期間被系統重新分類翻轉時提示(即使當下盈虧不明顯也提示重新評估)
- 賣方策略(如 sell put):達最大可能收益 50-80% 即提示提前平倉,不等到期,規避 pin risk

**Dashboard 燈號**:🟢正常 / 🟡接近觸發 / 🔴已觸發(標示觸發類型:價格/權利金/邏輯失效)

### 10.2 個股軌道(中長期,Phase 2 深化,v1 簡化版先行)

邏輯與選擇權完全不同:**不用短期價格 Z 值觸發,用「論點失效」觸發**。

| 觸發類型 | 邏輯 |
|---|---|
| 論點失效 | 進場時綁定明確書面「論點」(如「AI資本支出持續加速」),系統追蹤支撐論點的關鍵指標是否仍成立 |
| 相對強度惡化 | 個股相對板塊/大盤表現連續數週落後(用idiosyncratic return decomposition) |
| 估值階段目標 | 達到進場時設定的估值合理區間上緣,提示重新評估(非強制出場) |

**v1 簡化版**:先做「相對強度惡化」+「財報日提醒重新檢視」,完整論點追蹤系統列 Phase 2。

---

## 11. 每日歸因與覆盤引擎(只讀分析層)

每日收盤後 batch job:

1. **事實層**:用 Fama-French 風格單因子模型拆解當日報酬為總經/板塊/個股殘差貢獻
2. **歸因層**:比對 GDELT 事件庫 + 財報日曆,匹配當日異常報酬的可能成因
3. **模型對帳層**:比對模型開盤前(僅用T-1可得資料)的預測與實際結果,分類為:
   - 模型對、理由對 → 強化該邏輯路徑權重可信度
   - 模型錯、事後看屬不可預測雜訊 → 標記「不可預測案例」,不懲罰模型
   - 模型錯、但訊號事後看確實存在 → 反查特徵候選,產出可獨立驗證的新特徵假設

**輸出**:`attribution_log` 表(唯讀),欄位含日期/標的/預測方向/預測信心/實際報酬/歸因分解/事件標籤/判定類別。**產出永遠是「新特徵假設」,不是「對過去標籤的修正」——回測驗證必須用事件發生「當下」已存在的原始數值,不可用事後資訊,嚴防 look-ahead bias。**

---

## 12. 回測與驗證協議

- **歷史起點**:傳統市場/總經/資金流特徵抓 5-8 年;另類數據(論壇/GitHub/Trends)依實際可得歷史(可能僅2-3年),**不強行對齊長度**
- **分層驗證**:核心層(長歷史特徵)先跑完整 walk-forward;增量層(另類數據)在重疊期間做「加入後是否顯著提升」的檢定(bootstrap信賴區間,非單純比較 Sharpe 數值高低)
- **只有通過顯著性檢定的另類數據特徵才進生產模型**;未通過的保留在特徵庫,不刪除,待更多歷史累積後重新測試
- **驗證指標**:Directional accuracy vs baseline、Brier Score(機率校準度)、Sharpe/Sortino(非模型準確率本身)、高信心區間的 Precision
- **禁止全樣本回測後挑贏家**;採 rolling/expanding window walk-forward,依 regime 分開報告,不合併單一勝率
- **交易成本模型**:必須納入 slippage + transaction cost,理論勝率與實盤勝率分開報告

---

## 13. 排程自動化(Claude Code 雲端 routine)

**硬體限制認知**:24GB RAM 不足以支撐高品質本地模型的 agent 推理(官方建議需≥2台滿血Mac Studio或等值GPU rig),量化後的小模型對 prompt injection 防禦力弱,且系統會持續接觸不受信任的網路內容——**不在本地跑思考型模型**。

**雲端 Routine 架構**:
排程改用 Claude Code 的雲端 routine(cron 排程的 cloud agent),不依賴任何一台本機保持開機。每次執行都是全新的 session 與全新的 git clone,沒有本機資料庫持久性。`.duckdb` 檔案刻意不進 git,所以狀態必須透過 `data_sync/` 底下的 Parquet 檔案在 session 之間傳遞。

**雙軌同步機制**:
- **原始擷取資料同步**(`export_for_sync.py` / `import_from_sync.py`):負責唯讀、只新增的原始資料(OHLCV/新聞/選擇權快照等)。採 watermark + anti-join 機制,確保冪等性。
- **交易員聯盟狀態同步**(`export_league_for_sync.py` / `import_league_from_sync.py`):負責會被「更新」的狀態(判斷會被確認/撤回/評分、帳本現金會變動)。採整表覆寫策略,同步 session 之間的可變狀態。

**模型路由**:
```json
agents: {
  defaults: {
    model: { primary: "anthropic/claude-sonnet-5" },
    utilityModel: "anthropic/claude-haiku-4-5"
  }
}
```
- **Haiku 4.5(utilityModel)**:爬蟲後的淺層分類、情緒標記、關聯性判斷——高量、低思考任務
- **Sonnet 5(primary)**:經篩選後的少數關鍵內容做深度傳導邏輯分析

**安全規範**:僅用官方內建 skills 或自行撰寫的 skill,不安裝來路不明的第三方社群技能(已知存在惡意技能與 typosquatting 攻擊事件,系統會存放交易相關 API 金鑰,風險不可忽視)。

---

## 14. 技術棧

| 項目 | 選擇 | 理由 |
|---|---|---|
| 語言 | Python | 生態完整 |
| 資料處理 | polars(優先)+ pandas(相容) | 效能優於 pandas,適合日級批次量 |
| 模型 | LightGBM + scikit-learn(KMeans/GMM)+ HMM套件(hmmlearn) | 表格特徵最佳實務 |
| 儲存 | DuckDB(v1) | 本地零設定,分析型查詢效能佳;未來可升級 PostgreSQL |
| 排程 | Claude Code cloud routine / APScheduler | 每日batch job採 cron 排程 cloud agent,無需本機保持開機 |
| Dashboard | Streamlit(v1) | 開發速度最快,先驗證邏輯不雕前端;未來可換 FastAPI+React |
| Agent自動化 | Claude Code cloud routine(Sonnet 5 primary + Haiku 4.5 utilityModel) | 雲端 routine 排程爬蟲,雙軌 Parquet 同步 |

---

## 15. Claude Code 工作流程建議

**核心原則(官方文件)**:model 決定「知不知道」,effort 決定「有沒有盡力」。Claude 給了完整脈絡卻做錯 → 換更大模型;漏看檔案/沒跑測試/半途而廢 → 提高 effort,不是換模型。

| 階段 | 模型 | Effort | 備註 |
|---|---|---|---|
| 專案骨架/依賴安裝 | Sonnet 5 | medium | 粗活 |
| 資料 schema 設計 + 爬蟲串接 | Sonnet 5 | high | — |
| **Regime detection 雙軌驗證框架** | **opusplan**(Opus 4.8 規劃 + Sonnet 5 執行) | 規劃階段 xhigh | 全專案風險最高模組,錯了產生 look-ahead bias 或過擬合,值得規劃階段的溢價 |
| **Walk-forward 回測框架** | **opusplan** | 規劃階段 xhigh | 同上,防資料洩漏邏輯必須嚴謹規劃 |
| EV閘門 + Kelly部位計算 | Sonnet 5 | high | 數學公式明確,不需Opus |
| 每日歸因覆盤引擎 | Sonnet 5 | medium-high | 資料處理與匹配邏輯 |
| 選擇權/個股風控模組 | Sonnet 5 | high | 邏輯已在本文件定義清楚,實作為主 |
| Dashboard(Streamlit) | Sonnet 5 | medium | UI迭代用plan mode反覆調整,不堆effort |
| Cloud routine/Parquet 同步設定 | Sonnet 5 | medium | 標準設定檔工作 |
| 除錯/測試 | Sonnet 5 | high,卡住才升級Opus | 先加效果,真的是能力不足才換模型 |

**建議操作流程**:
1. 每階段先用 plan mode(Shift+Tab)——只讀不動手,先確認計畫
2. 人工審查計畫,**regime detection 與回測框架這兩塊必須親自把關 look-ahead bias**
3. 確認後才切執行模式
4. 要求 Claude 提供證據而非自稱成功(實際測試輸出、跑過的指令)

---

## 16. 待決定/待資料驗證的參數清單(不是拍板,是待優化)

以下數值皆為 v1 起點,非最終值,待歷史回測與實盤驗證後透過網格搜索/貝氏收縮校準:

- [ ] 選擇權風控:Z值停損門檻(v1: -2)、權利金停損(v1: -50%)、固定停利倍數(v1: +100%)、賣方提前平倉比例(v1: 50-80%)
- [ ] EV閘門:滾動窗口長度(v1: 90天)、可交易分位數門檻(v1: 前25%)
- [ ] Kelly保守係數(v1: 0.25-0.5,待回測校準具體值)
- [ ] 每日虧損熔斷百分比門檻(尚未定案,取決於使用者風險承受度,需與使用者確認)
- [ ] 選擇權微結構資料源:v1免費估算版 vs 付費訂閱(ORATS/CBOE),待免費版驗證方法論有效性後再評估升級
- [ ] Regime detection 狀態數(v1固定3個,是否需要更多待驗證後決定)

---

## 17. 明確不做的事(避免範圍蔓延)

- 不接券商 API 執行訂單
- 不做全市場橫截面掃描(Phase 1 僅限 watchlist 核心清單 + 次要觀察名單)
- 不在 Phase 1 建立完整個股論點追蹤系統(僅簡化版)
- 不在 Phase 1 建日內即時串流系統(僅每日批次)
- 不安裝來路不明的第三方社群技能
- 不在本地(24GB RAM)跑思考型 agent 模型

---

## Agent skills

### Issue tracker

Issues live in GitHub Issues for `daniel1001a/stockmoney`, via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five canonical labels (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`), unmapped. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
