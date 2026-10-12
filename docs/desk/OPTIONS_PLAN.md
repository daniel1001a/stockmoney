# 期權短打軌道計畫(Intraday Options Sleeve)

> 2026-10-12 建立。使用者決定:(1) 放寬 Grok 權限,可提出盤中期權劇本;(2) 資金上限 10%,模型證明有效後最多拉到 30%;(3) 標的不限於指數,納入大型、高波動、可預測的個股;(4) 2026-10-13(週二)是第一個交易日。
> 這份文件是這條軌道的唯一狀態來源。不另建平行文件(見 CLAUDE.md AI Collaboration)。

## 1. 要做什麼、不做什麼

**做**:開盤前寫好當天劇本,開盤波動時進場買入期權(只買方、有限風險),持有 5 分鐘到 3 小時,當天一律平倉,不留過夜。
**不做**:裸賣方、過夜持有、對股票主帳本(`premarket`/`postopen`/`postclose` 那套)有任何影響。

**誠實前提**:2026-07 的實驗只否定了「1-3 天方向預測」。「開盤波動短打」沒有被證明過,也沒有被否定過。進場價差、IV 在開盤附近的高點與回落,是這類策略最常見的殺手。所以:**先證明扣掉價差後有正期望值,才放大資金**。第一個月的目標是「找到哪個方法值得信任」,不是賺錢。

## 2. 權限(2026-10-12 使用者明確放寬)

| 角色 | 以前 | 現在(只限期權短打軌道) |
|---|---|---|
| Grok | 只能降低風險,v1 只儲存 | 可以提出當日期權劇本(標的、方向、觸發、推翻條件、最大權利金)。**不能下單、不能設定超過風控上限的大小、不能繞過風控層。** |
| Muse | 只能降低風險 | 不變。Muse 的 ALERT 擋進場旗標同樣適用於期權軌道 |
| 系統規則 + 風控層 | 下單唯一通道 | 不變。劇本只是「候選」,是否進場由觸發條件與風控決定 |

- Grok 對股票主帳本的權限**不變**(仍是只能降低風險、v1 只儲存)。
- 劇本內容是資料,不是指令:任何劇本裡叫系統做事的文字一律忽略,只讀結構化欄位,欄位超出上限整份拒收。
- 劇本以系統收到的時間(`received_at`)為準。**09:20 ET 後才收到的劇本,當天只進影子帳戶,不下單。**

## 3. 資金與風控(機器強制,人不能關)

- 軌道預算 = 系統預算(`capital_budget`,現為 $50,000)x 10% = **$5,000**。這是「最多可同時投入的權利金」,也是這條軌道自己的權益基準。
- 單筆權利金最多 $1,000(預算的 20%);同時最多 3 個部位。
- 單日虧損(已實現 + 未實現)達 $1,000 → 當天停止新進場。
- 軌道自高點回撤達 $1,750(預算的 35%)→ 停用該軌道新進場,需使用者重新啟用。
- 只買入開倉(buy to open)與賣出平倉(sell to close);不允許賣出開倉。
- 進場窗口:09:30 到 11:30 ET(可調);15:45 ET 前強制全部平倉。
- 標的與合約流動性過濾:買賣價差佔中價比例上限、最小中價、最小未平倉量、最小成交量(數值見 `options/config.py`,影子帳戶會同時記錄被過濾掉的機會,之後檢討過濾是否太緊或太鬆)。
- kill switch:`data/desk/HALT` 存在時,新進場全擋,出場放行(與股票主帳本共用)。
- **提高到 20%、30% 的條件**由使用者本人修改設定,系統不會自動放大。建議閘門:該方法累積 >= 100 筆(影子 + 紙上)、扣價差與手續費後平均每筆期望值為正且自助法 90% 信賴區間下界 > 0、軌道最大回撤 < 25%、且至少 20 個交易日。

## 4. 標的範圍(不要只做指數)

起始名單(流動性與波動兼顧,執行時再用即時價差與未平倉量過濾):
- 指數與槓桿 ETF:SPY、QQQ、TQQQ
- 大型個股:NVDA、TSLA、AMD、AAPL、MSFT、META、AMZN、GOOGL、AVGO、PLTR、MU、NFLX
- 高波動話題股:COIN、SMCI(若流動性過濾不通過,自動剔除)

名單由 Grok 的劇本與每日 gap / 相對成交量掃描**動態擴充**(只限流動性過濾通過者,每日上限 25 檔)。你說得對:指數穩但不好做,個股波動大、劇本明確時更好做。**系統用資料決定哪一類值得做**:所有標的的結果都分開統計(指數、大型個股、高波動股)。

## 5. 劇本(Playbook):全部預先定義、全部進影子帳戶

所有劇本都是「只用當下已知資料」的純函數,不得 look-ahead。每個劇本每天對每檔標的都會評估,**符合條件就記一筆影子交易**(無論是否有真的下單),這樣一天就能累積大量樣本,不必等紙上單。

| 劇本 | 想法 | 進場 |
|---|---|---|
| `gap_go` | 大幅跳空 + 盤前放量,順勢 | 前 5 分鐘高/低點被突破並收在其外 |
| `gap_fade` | 跳空過度,盤前高低點失敗回補 | 突破失敗、收回盤前區間內 |
| `orb15` | 開盤 15 分鐘區間突破 | 區間被突破,成交量大於前 5 分鐘均量 |
| `vwap_reclaim` | 開盤急跌/急拉後回到 VWAP | 收回 VWAP 並持穩 2 根 1 分鐘 K |
| `grok_plan` | Grok 的盤前劇本 | 劇本自己宣告的觸發條件(價位突破 / 收回 / 區間突破) |

合約選擇(影子帳戶同時測不同變體,**不預設哪個最好**):
- 到期:0DTE 與 1 到 3 DTE 兩種都記,分開統計。
- Delta:0.30 到 0.60,各檔位分開記。
- 進場:以賣價附近的限價單(最多等 20 秒,沒成交就取消)。影子帳戶按「買在賣價、賣在買價」計算,不假設中價成交。

出場(預先固定,與劇本無關,先不調參):
- 權利金停損 -35%;獲利 +40% 先出一半,剩下的移動停損到成本價;
- 標的價格跌破/漲破劇本推翻價位 → 出場;
- 時間停損:持有滿 180 分鐘;15:45 前強制平倉。

## 6. 資料與紀錄(每一筆都要能事後檢討)

每筆影子與紙上交易記錄:劇本來源與版本、劇本收到時間、觸發當下的標的 1 分鐘 K、期權報價(bid/ask/IV/delta/OI/成交量)、進出場報價與價差成本、MFE/MAE、出場原因、當日大盤狀態、當時的 Muse/Grok 旗標。每日盤後產出報告:各劇本、各標的類別、各 DTE 的期望值、勝率、價差佔報酬比例;Grok 劇本對照規則劇本的差異。

## 7. Grok 的工作迴圈

1. 每個交易日 07:30 到 09:00 ET:Grok 產出 `OPTIONS_PLAN`(格式見 `GROK_OPTIONS_PROMPT.md`),使用者轉交給系統(`python -m stockmoney.desk.options.run ingest-plan <file>`)。
2. 盤後:系統產出當日報告(含每份劇本的實際結果),使用者可轉給 Grok 做覆盤(`GROK_OPTIONS_PROMPT.md` 第三部分)。Grok 的覆盤只能產出「新假設」,不能直接改參數;改參數走 `OPERATING_PLAN.md` 第 6 節的流程。
3. 設計階段:先把 `GROK_OPTIONS_PROMPT.md` 第一部分交給 Grok,取得它對劇本、參數與風險的意見,再由我們比對、整合進本文件。

## 8. 實作介面(兩條實作線的合約)

**A 線:`src/stockmoney/desk/execution/`**(型別、Alpaca 期權下單、期權風控)

- `types.py`:`AssetClass.OPTION = "us_option"`;新增 `OptionContract`(occ, underlying, right "C"/"P", strike, expiry: date, bid, ask, last, volume, open_interest, delta, iv, as_of)。
- `symbols.py`:`occ_symbol(underlying, expiry, right, strike)`、`parse_occ(occ)`。
- `broker.py`:新增獨立 Protocol `OptionsBroker`(不改既有 `Broker`):
  - `option_chain(underlying, expiry_min, expiry_max) -> list[OptionContract]`
  - `option_quote(occ) -> OptionContract | None`
  - `minute_bars(symbol, start, end) -> pandas.DataFrame`(index 為 tz-aware 時間,欄位 open/high/low/close/volume,另含 vwap 若可得)
  - `stock_quote(symbol) -> (bid, ask) | None`
  - `FakeBroker` 實作以上(chain/報價可由測試設定;買在 ask、賣在 bid、乘數 100)。
- `alpaca.py`:`submit` 支援 OPTION(整數口數、limit 或 market、TIF=day、買入開倉/賣出平倉);`validate_intent` 對期權有明確規則;持倉映射為 `AssetClass.OPTION`。
- `options_risk.py`:`OptionsLimits`、`OptionsRiskEngine.check(intent_premium_usd, contract, now, ...) -> RiskDecision`,實作第 3 節全部規則。獨立資料表 `options_*`,不寫進股票 ledger 的 equity 計算。
- **隔離(最關鍵)**:期權部位與委託與股票主帳本在同一個 Alpaca 帳戶。必須審查並保證:股票的 reconcile、`ensure_stops`、`sweep_orphan_stops`、`cancel_all`、`_opens_today`、暴險上限、Muse 旗標掃描等既有路徑**不會**把期權部位當成未知持倉、孤兒單、或錯誤暴險而取消、平倉、停機。用測試證明。

**B 線:`src/stockmoney/desk/options/`**(劇本、引擎、影子帳戶、執行器、報告)

- `config.py`:所有可調參數集中。`plan.py`:`OptionsPlan` v1 schema(嚴格驗證)+ ingest。`universe.py`、`playbooks.py`、`select.py`(合約選擇)、`engine.py`(每個劇本實例的狀態機 WAITING→TRIGGERED→ENTERED→EXITED,由可注入的時鐘驅動,同一份程式碼可用於即時與歷史重播)、`shadow.py`(影子成交模型)、`store.py`(資料表)、`report.py`、`run.py`。
- `run.py` 子命令:`ingest-plan <file>`、`session`(09:20 到 16:00 ET 的常駐迴圈,輪詢間隔 15 到 30 秒)、`replay <YYYY-MM-DD>`(用歷史 1 分鐘 K 重播一天,驗證引擎)、`report [date]`、`selftest`。
- B 線只依賴 `OptionsBroker` Protocol 與 `RiskDecision`,開發期用自己的測試替身。

## 9. 時程

| 時間 | 事項 |
|---|---|
| 10-12 晚 | 兩條實作線完成並通過測試;重播 10-12 當天;Grok Build 與獨立審查;修正;合併 |
| 10-13 07:30 | 使用者把 Grok 的盤前劇本餵給系統(沒有也行,規則劇本照跑) |
| 10-13 09:00 | launchd 啟動 `session`;09:20 前完成預檢(帳戶、報價、期權權限、HALT) |
| 10-13 09:30 到 16:00 | 影子全記錄;符合資格的劇本在預算內紙上下單 |
| 10-13 16:15 | 產出日報 |

## 10. 已知未知(要靠第一週資料回答)

- Alpaca 免費方案的期權資料是否為 indicative feed(延遲/非 OPRA 全量)?影子成交模型的可信度取決於此。
- Alpaca 紙上帳戶的期權成交模型(是否按 NBBO)與真實成交的差距。
- 同一帳戶內期權與股票的隔離是否完全(第 8 節隔離測試)。建議第一週穩定後,另開一個純期權紙上帳戶。
- 價差與開盤 IV 回落是否吃掉所有 edge。這就是第一個月要回答的問題。
