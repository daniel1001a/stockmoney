# 重建計畫 — 從「5 天技術模型」到「1–3 天消息驅動選股 + 盤前劇本引擎」

> 狀態:**書面計畫,尚未開工**。等使用者確認第 8 節的 baseline 門檻定義後,才動 Phase 0。
> 決策原則(使用者定):**先做時光機回測 → 用數據決定留還是打掉。** 一切服從終極目標:**賺錢,並且每天用可驗證的勝率證明。**

---

## 0. 這份計畫怎麼用

三個使用者已拍板的前提:

1. **順序**:先建「時光機回測(脊椎)」量測現行模型的真實勝率。
   - 若現行模型的 OOS 風險調整後表現**明顯打不過市場平均交易 baseline** → **打掉重練**(心臟本來就裝錯,不留)。
   - 若**差不多或略勝** → **保留骨架(資料層/回測/league/DuckDB),只換心臟**(5 天→1–3 天、加消息融合、加 discovery、加盤前劇本)。
   - **無論哪條路,脊椎都留下來** — 它是之後每天證明勝率的工具。
2. **即時數據**:現階段走**免費 EOD + 盤前劇本 + 人工執行**;未來若需要,再評估付費 intraday(Polygon)。設計要預留這個升級位,但不現在花錢。
3. **執行**:先交這份完整書面計畫,確認後才派工頭/工人開工。

**硬邊界(不因任何後續需求改變)**:系統只出「分析 + 劇本 + 機率 + 理由」,**永不下單**。就算未來接 Robinhood,按下執行的永遠是人。這同時是 CLAUDE.md 的邊界,也符合現階段「系統出計畫、人裁量」的需求。

---

## 1. 現況診斷(附程式碼證據)

| 使用者抱怨 | 程式碼真相 | 檔案 |
|---|---|---|
| 股票池是死的,不會自己篩 | `watchlist_members` 是固定表,首頁做的是「分析固定清單」,無 discovery | `src/stockmoney/data/watchlist.py` |
| SOXL/SOXS 一定相反,呈現很笨 | SOXL/SOXS/SOXX 共用同一組半導體 dispersion 特徵 = 同一訊號卻各佔一行 | `models/production.py:16-21` |
| 像技術分析,消息面沒體現 | GMM regime + logistic 吃 EOD 技術特徵;GDELT 情緒 ablation 測不顯著、沒 promote | `models/production.py`, commit `0d2aa8b` |
| 盤前給不出判斷 / 沒有交易計畫 | `DEFAULT_HORIZON = 5` — 5 天 EOD 方向模型,輸出單一 P(漲/跌/盤),非盤前 if-then 劇本 | `models/production.py:43` |

**核心錯配**:打造的是嚴謹的 5 天 EOD 技術模型;需要的是消息驅動、1–3 天、自己選股並給盤前劇本的引擎。骨架好,心臟裝錯。

---

## 2. Lin(順勢而為)打法 DNA → 系統需求

從 400+ 則貼文濃縮,每條直接對應一個系統需求:

| Lin 的打法 | 轉成系統需求 |
|---|---|
| 消息驅動:「現在市場很多都是消息驅動」(Trump/伊朗/油價/財報) | 支柱三:新聞事件圖當引擎,不當註腳 |
| 極度集中「弱水三千,只取一瓢」 | 支柱一:宇宙不大,但每天只浮現有 edge 的少數 |
| 一切錨定關鍵價位 + if-then(6500 守/破;NBIS 113 突破才追) | 支柱二:盤前情境樹劇本 |
| regime 決定武器(震蕩→sell put;趨勢→買 call;情緒差→空手) | 支柱二 + 支柱四:策略隨 regime 切換 |
| 0–3 DTE、快進快出(盈利30%走、虧損20%先走) | 目標週期 = 1–3 天;風控快進快出 |

---

## 3. 目標架構:4 支柱 + 1 脊椎

```
                          ┌─────────────────────────────────────────┐
資料層(保留:紀律/時間戳)  │ 每筆記錄 available_ts,防 look-ahead      │
                          └─────────────────────────────────────────┘
                                            ↓
支柱三:新聞事件圖引擎  →  多源聚類成 EVENT(去重)→ 評[方向/意外/可信/傳導ticker&sector/半衰期]
                                            ↓
支柱一:Discovery 漏斗  →  宇宙(去冗餘,SOXL/SOXS/SOXX→一個 beta 向量)→ opportunity score 排序 → 今日精選 vs 觀望
                                            ↓
支柱二:盤前劇本引擎    →  每個精選出情境樹:現價+關鍵價位, if 開高站上X→買call(目標/停損/機率/倉位), if 跳空低→sell put/觀望
                                            ↓
支柱四:聯賽=實驗室    →  多引擎(技術/消息/順勢/均值回歸/收租)用真實1-3d期權P&L分regime&sector計分 → 贏的餵production
                                            ↑
─────────────────────────────────────────────────────────────────────────────
脊椎:時光機回測 replay harness(FIRST,決策閘門,永久保留)
      挑歷史日D→只用 available_ts<D開盤 的資料→跑完整漏斗→前滾1-3d算勝率/EV/Brier,分regime/sector/setup型
```

---

## 4. Phase 0 — 時光機回測 = 生死閘門(**第一個、也是唯一先做的**)

**目的**:誠實量測現行 5 天模型的 OOS 表現,對照市場平均交易 baseline,執行第 0 節的留/打決策。

**很大程度是「整合現有零件 + 加 baseline + 誠實報告」,不是從零打造** — repo 已有 `walk_forward.py`、`backtest_options_pnl.py`、`backtest_direction_models.py`、`grading_options.py`、`metrics.py`(Brier)。所以 Phase 0 小而快。

### 4.1 報告卡指標(全部 walk-forward OOS、含交易成本、分 regime 不合併)
- 方向準確率 directional accuracy
- Brier score(機率校準)
- 模擬交易勝率 win rate(模型方向 + 1–3d 持有 + v1 停損停利規則)
- avg win / avg loss、payoff ratio
- EV per trade(扣成本)、Sharpe / Sortino
- 高信心區間 precision

### 4.2 要打敗的 baseline(這就是「市場平均交易勝率」的操作型定義)
- **B0 硬地板**:50% coin flip(方向)。
- **B1 always-long**:永遠偏多買 call。反映股票長期上漂,誠實的市場 floor。
- **B2 random-entry 短天期 long option**:隨機日、隨機標的、隨機買一張 1–3DTE ATM 期權的平均下場。**最貼近使用者說的「一般人隨便交易的市場平均勝率」**(理論上因 theta 是輸的)。
- **B3(stretch)**:一個非平凡但笨的動能/價位規則。

### 4.3 決策規則(對應使用者原話)
- 現行模型 OOS 風險調整後**明顯不如 B1/B2** → **打掉重練**。
- **差不多或略勝** → **保留骨架換心臟**。
- 判定用**風險調整後**指標(EV/Sharpe after costs),**不看原始勝率**(CLAUDE.md §12:禁止超緊停利製造虛假高勝率)。

### 4.4 資料紀律
沿用既有 `feature_matrix` disjointness 保證 + `available_ts` 紀律 + `walk_forward` OOS 折疊。**不新增任何繞過時間戳的路徑。**

### 4.5 交付物
- `scripts/timemachine_report.py` → 產出一張**現行模型報告卡**,分 regime、含所有 baseline 對照。
- 一段誠實結論:留 or 打。

### 4.6 Phase 0 結果(2026-07-13)— 判決:打掉建模層心臟
跑 `STOCKMONEY_DB=data/stockmoney_live.duckdb .venv/bin/python -m scripts.timemachine_report`(8y SOXL OOS):

| 策略 | n | 勝率 | 每筆平均 | 95% CI | 3類方向準確率 |
|---|---|---|---|---|---|
| 現行模型 | 240 | 39.2% | −3.78% | [−13.2%,+5.3%] | 0.362 |
| B1 永遠買 call | 990 | 42.5% | +4.15% | [−1.0%,+9.1%] | 0.368 |
| B2 隨機買期權 | 24,750 | 37.0% | −5.09% | [−5.9%,−4.3%] | ~chance |

- 模型只險勝隨機(不顯著),被裸多 B1 電爆 8pp,3類方向準確率≈隨機。
- 目標 1-3 天更慘:h=2 −5.78% CI[−11%,−0.6%] **顯著虧損**;h=3 −7.91%。
- EV 閘門是反的(放行 −10.75% / 擋掉 +1.68%)。
- B1 的 +4.15% 有半導體牛市紅利成分,非可照抄策略,但證明「連順勢裸多都贏過模型」→ 印證 Lin 順勢方向對、EOD 技術方向模型錯。

**決策**:打掉建模層(regime 方向模型 + EV 閘門);保留資料層 / walk_forward / option-P&L 引擎 / league。進入 Phase 1 建新心臟。

### 4.7 Phase 1a 結果(2026-07-13)— 順勢 trend+regime 心臟
`scripts/timemachine_report.py` 已含順勢引擎(規則式、leak-free 動能 + adx regime 閘門)+ robustness 面板。mean option return(扣成本):

| horizon | 舊模型 | B1 裸多 | B2 隨機 | 順勢 TREND (L20/adx20) |
|---|---|---|---|---|
| 2 天 | −5.78%*(顯著虧) | −0.64% | −4.74% | −1.28%(贏B2/輸B1/ns) |
| 3 天 | −7.91% | +1.34% | −4.45% | +0.45%(贏B2/輸B1/ns) |
| 5 天 | −3.78% | +4.15% | −5.09% | +3.51%(贏B2/輸B1/ns) |

- 順勢心臟每個 horizon 都贏隨機、優於舊模型(舊模型 h=2 顯著虧損 → 順勢拉回近打平)。
- **adx regime 閘門一致優於不設閘門** → 「震蕩空手」紀律用 8y 驗證成立。
- 但打不過裸多 B1、無一顯著獲利。主因:2018-26 SOXL 是 3x 槓桿超級牛市,單一向上標的上「無腦買 call」近乎無敵。
- **卡點洞見**:單一標的有天花板。順勢真 alpha = 橫向挑最強(錢往最強處去),需要**廣度**。這使原本排最後的「橫向 discovery」提前 — 數據驅動的重排序。

### 4.8 Phase 1b 結果(2026-07-13)— 橫向選股:負結果
`scripts/xsec_trend_backtest.py`,宇宙 = 9 檔 8y 單名股。mean option return:

| horizon | XSEC 挑最強 top1-3 | B1_EW 全買 9 檔 | B1_SOXL 裸多 | B2 隨機 |
|---|---|---|---|---|
| 2 天 | ~−2.0% | −1.35% | +1.45% | −4.79% |
| 3 天 | −0.5〜−1.0% | +0.35% | +4.93%* | −4.67% |
| 5 天 | +1.1〜+2.2% | +3.29%* | +10.72%* | −5.01% |

**「挑最強」不但沒加值,反而輸給「無腦全買」。動能選股在 2-5 天沒預測力。Discovery-via-momentum 前提不成立。**

### 4.9 三實驗貫穿鐵律
Phase 0(舊模型輸)+ 1a(順勢打不過裸多)+ 1b(選股減分)→ **在此 8y 樣本,純價格/技術訊號沒一個打得過單純做多。** 印證使用者 point 3:短期交易消息面>技術面。剩唯一未測 = 新聞/情緒/催化。

**戰略旗標**:長期買方期權結構性低勝率(靠 theta 吃虧);若要「穩定高勝率」,premium-selling(sell-put/credit)結構上更契合 —— 數據一再指向它,雖使用者因資金暫緩。

### 4.10 Phase 1c 結果(2026-07-13)— premium-selling:第一個顯著淨正
`scripts/premium_selling_backtest.py`,賣 OTM put,對抵押資本報酬:

| 策略 | 勝率 | 每筆報酬 | 顯著 | 最慘 | 同期做多 |
|---|---|---|---|---|---|
| 5%OTM/5td | 90.4% | +0.05% | ✅ | −46% | +0.53% |
| 5%OTM/10td | 85.6% | +0.11% | ✅ | −63% | +1.01% |

- **第一個 95% CI 顯著淨正的策略**,勝率 78-94%,直接命中「穩定高勝率」。
- 但對資本報酬極小、被單純做多海放 5-10x;尾端 −46〜−63% 兇。
- 兩個利多但書:IV 代理(realized×1.1)低估真實權利金(VRP,真 IV 更高→賣方收入更多,是投資真實資料的理由);未做停利+regime閘門(Lin 兩者都做→砍尾端)。
- **綜合**:賣方是「穩定高勝率」心臟候選;弱點(報酬小、尾端兇)正好是新聞/regime 閘門能修。

### 4.11 實驗三(新聞)的現實與重新定位
新聞資料:news_items 122筆/news_articles_raw 587筆(僅~1.5yr,無法嚴謹回測);**event_news_gdelt 4210筆 2014-2026(唯一有11yr歷史,tone_score+goldstein)**;其餘表空。GDELT 對「方向」已測過不顯著(commit 0d2aa8b)。**重新定位**:GDELT 不當報酬預測器,當賣 put 的**風險閘門** — 測「負面語氣/事件強度能否標記尾端下跌日」,即 賣put+新聞閘門 vs 裸賣put。使用者指示:先實驗二(done)、不管如何也做實驗三。

### 4.12 Phase 1c-gated + 總結(2026-07-13)
賣 put + regime 閘門(close<SMA50 或 rv>6mo-80pct 就空手):尾端 −46/−63% → −32/−34%(小三成),勝率微升,**但報酬歸零、不再顯著**。揭露:賣方那點正報酬正是來自「恐慌時賣」,權利金與尾端無法用價格閘門便宜分離(VRP 本質)。

**四實驗總結:8y、純價格訊號,沒有一個明顯打贏「單純做多」。** 舊模型輸/順勢輸裸多/選股減分/賣方顯著但極小且加閘門歸零。唯一未測真槓桿=真實新聞,被資料擋死(GDELT 假、真新聞僅1.5yr)。

**價值重定位**:(1) 擋下用虧損模型交易+排除3個空策略=省錢;(2)「裁判」心臟已跳=時光機 harness 能審判任何新想法(使用者要的「證明勝率」引擎);(3) 剩唯一槓桿(新聞)卡點=資料,解法=回填真實歷史。

### 5-bis. 工人階段起點(修訂)
不再是「新心臟已證明→包產品」。現實是:**先解資料卡點,才知道有沒有 edge**。故:
- **Worker-1(資料,最高優先)**:回填真實 GDELT(免費可回填2015)+ 真實 per-symbol 新聞歷史。解鎖實驗三。
- **Worker-2(裁判)**:把 timemachine_report/xsec/premium 三個回測固化成一個「每日勝率記分板」永久 harness + 測試。
- 產品工人(discovery UI / 盤前劇本 / dashboard)延後到「新聞資料回填後、有 edge 通過 harness」才啟動。

---

## 5. Phase 1+(條件式:視 Phase 0 結果)

> 若 Phase 0 判定「換心臟」,依序落地四支柱;若判定「打掉重練」,四支柱一樣是藍圖,只是資料層以外全部重寫。內容相同,差在保留多少舊碼。

### 支柱一 — Discovery 漏斗
- **介面契約**:`discover(as_of) -> list[Candidate{symbol, opportunity_score, components{news, unusual_options, rel_strength, regime_fit, hist_winrate}, dedupe_group}]`
- **去冗餘**:SOXL/SOXS/SOXX → 單一 `semiconductor_beta` 向量;每個 dedupe_group 只出一個代表。
- **測試**:同一 dedupe_group 不重複出現;score 單調性;無 look-ahead(只吃 `available_ts<as_of`)。
- **里程碑**:先在半導體 + Lin 名單跑通,再擴 sector。

### 支柱二 — 盤前劇本引擎
- **介面契約**:`playbook(symbol, as_of) -> ScenarioTree{current, levels[], branches[Scenario{trigger, instrument(buy_call/buy_put/sell_put/wait), target[], stop, probability, sizing}]}`
- **機率來源**:歷史同型 setup + regime 的 base rate(由脊椎回測產生),**不瞎編**。
- **測試**:分支涵蓋 gap-up/down/flat;機率總和合理;每分支有明確 invalidation 價位。

### 支柱三 — 新聞事件圖引擎
- **介面契約**:`event_graph(as_of) -> Events[Event{cluster_id, direction, surprise, credibility, affected[ticker/sector], half_life}]` + `ticker_sentiment(symbol, as_of)`
- **重點**:聚類去重、衝突事件互相抵消、一事件傳導多標的、半衰期衰減。產出 per-ticker + 大盤情緒狀態 + 一段像 Lin 晨報的**每日總結敘事**。
- **驗證**:用**短天期、event-conditioned** 重測 ablation(先前在 5 天測不顯著,很可能在 1–3 天顯著)。bootstrap 信賴區間,過檢定才進 production(CLAUDE.md §12)。

### 支柱四 — 聯賽=實驗室
- 多引擎(技術/消息/順勢模仿/均值回歸/sell-put 收租)用**真實 1–3d 期權 P&L、分 regime/sector** 計分。已有 `league/` + `grading_options.py`(Wave D)。
- **擴充**:regime/sector-conditional 排行榜;每種條件下的贏家餵 production。

### 脊椎(永久)— 修正迴圈
- replay harness 抽 100+ 天,**分層覆蓋每種盤**(趨勢多/空、震蕩、高 VIX、財報日、地緣衝擊)。
- miss 歸因 → 新特徵/規則候選 → 人工審核 → 重測。已有 attribution 引擎可接。

---

## 6. 工頭 / 工人 分工與契約

- **工頭 = 主 session(我)**:擁有架構、模組間介面契約、整合與驗收。
- **工人 = 背景 subagents(worktree 隔離,互不干擾)**,各包一個可獨立測試的模組:
  - Worker-Backtest(脊椎,**Phase 0 先跑**)
  - Worker-Discovery / Worker-Playbook / Worker-News / Worker-League(Phase 1+)
- **防資訊遺失的機制**:每個工人交付 = **介面契約(第 5 節那些簽名)+ 通過的測試**。工頭跑他們的測試 + 整合 harness 驗收。**契約寫在檔案裡,不靠口頭傳遞** — 這就是使用者擔心的「兩 agent 間資訊遺失」的解法。

---

## 7. 邊界與風險

- **不下單**、不給個人化「買這支」建議;產品內出結構化決策支援,聊天裡不喊單。
- **即時數據缺口**:盤前劇本在 EOD+隔夜+新聞上可行;「盤中即時反應」需付費 intraday,現階段用人工執行補。
- **過擬合風險**:小樣本(另類數據僅 2–3 年);嚴守 walk-forward + bootstrap 顯著性,分 regime 報告,不合併單一勝率。
- **blogger .md 打不開**:`Tobias_Johnson_Posts.md` 被 macOS Messages 隱私鎖住(需 Full Disk Access)。已從兩個 JSON 取得足夠素材;若 .md 有額外內容,請以一般檔案重新分享。

---

## 8. 需要你拍板的一件事

Phase 0 的**生死門檻**要精確。我建議:

> 現行模型必須在 **EV-after-costs 打贏 B2(random 短天期期權買方)**,且 **方向準確率打贏 B1(always-long base rate)**,判定用風險調整後指標。達不到 → 打掉重練;達到或略勝 → 保留骨架換心臟。

確認這個定義(或調整門檻),我就派 Worker-Backtest 開跑 Phase 0。
