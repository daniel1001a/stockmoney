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

| 劇本 | 想法 | 進場(過濾版,2026-10-12 依 Grok 設計諮詢修訂) | 時間窗 |
|---|---|---|---|
| `gap_go` | 有催化劑、放量的跳空,順勢 | 個股跳空 >= 1.5%(SPY/QQQ/TQQQ >= 0.5%)且 RVOL >= 2;1 分鐘 K 收在開盤前 5 分鐘高/低之外(跳空方向),觸發 K 成交量 >= 前 5 根均量。推翻:回到開盤價,或跌破/漲破觸發 K 的低/高 | 09:35 到 10:15 |
| `orb15` | 開盤 15 分鐘區間突破 | 區間 09:30 到 09:45 被突破,突破 K 成交量 > 前 5 根均量 1.5 倍;標的自開盤報酬與基準(科技成長用 QQQ,其餘 SPY)同向且不落後;當日 RVOL >= 1.5。推翻:收回區間內或反向突破 | 09:45 到 11:00 |
| `vwap_reclaim` | 急跌/急拉後收回 VWAP(僅影子) | 收回 VWAP 並連續 2 根 1 分鐘 K 站穩,且 VWAP 斜率不平 | 09:45 到 11:30 |
| `gap_fade` | 跳空過度回補(僅影子) | 突破失敗、收回盤前區間內 | 影子 |
| `grok_plan` | Grok 的盤前劇本 | 劇本自己宣告的觸發條件。只有 playbook 為 gap_go 或 orb 且標的 RVOL >= 1.5 才能下紙上單,否則只進影子 | 劇本自訂 |

所有劇本 09:35 前不進場。紙上實單資格(第一天):過濾版 `gap_go`、`orb15`、符合條件的 `grok_plan`。其餘只進影子。

合約選擇(影子帳戶同時測不同變體,**不預設哪個最好**):
- 到期:實單用 1 到 3 DTE(最近且 DTE >= 1);0DTE 記為影子變體,分開統計。
- Delta:指數 ETF 0.40 到 0.55,個股 0.45 到 0.60。ATM 價差 <= 權利金 8%。
- 進場:以賣價附近的限價單(最多等 20 秒,沒成交就取消)。影子帳戶按「買在賣價、賣在買價」計算,不假設中價成交。

出場(預先固定,與劇本無關,先不調參):
- 變體 A(實單):權利金停損 -35%;獲利 +40% 先出一半,剩下的移動停損到成本價;
- 變體 B(影子):權利金停損 -30%;+40% 出一半後,剩餘用標的 1 分鐘 ATR(14) 的 0.4 倍追蹤;兩種變體分開報告,不混用;
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

### A 線實作紀錄(2026-10-12,`scripts/options_data_check.py` 在紙上帳戶實測,未下任何單)

- **期權資料 feed**:此帳戶拿到的是 `indicative`。明確要求 `opra` 回 `OPRA agreement is not signed`。不指定 feed 時 Alpaca 自動給 indicative(Alpaca 文件:indicative 的成交是延遲的、報價是經修改的)。影子成交模型目前只能以 indicative 報價計算。
- **期權 1 分鐘 K 拿不到**:`/v1beta1/options/bars` 沒有 feed 參數,此帳戶查詢回 `OPRA agreement is not signed`(HTTP 403)。`minute_bars(occ, ...)` 因此回傳空表並記錄警告。B 線的 MFE/MAE 與重播若需要期權 K 線,只能改用輪詢快照,或由使用者簽署 OPRA 協議後再驗證(是否需付費未驗證)。
- **快照內容**:報價(bid/ask)齊全;IV 與 delta 只有部分合約有(SPY 10-13 到期 302 檔中 117 檔,NVDA 10-14 到期 106 檔中 36 檔,深價內外多半沒有)。當日成交量取自快照的 dailyBar(日期不是今天就記為 0);未平倉量取自交易 API 的 contracts 端點(每日更新一次,約七到八成合約有值)。
- **股票資料**:IEX 即時可用(成交量只是 IEX 的份額,不是全市場量);近期 SIP 回 `subscription does not permit querying recent SIP data`。
- **帳戶期權權限**:options_approved_level=3、options_trading_level=3。
- **與第 8 節的差異**:`OptionsRiskEngine.check` 的簽名改為 `check(intent, contract, snapshot, now, *, open_orders=None, marks=None, blocked=None)`(收整張 `OrderIntent` 而不是 `intent_premium_usd`,以便依口數縮單並檢查限價)。`OrderIntent` 未新增欄位:期權單的 `side` 買即 buy_to_open、賣即 sell_to_close,由 adapter 帶上 `position_intent`。`BrokerOrder` 新增可選欄位 `asset_class`。
- **隔離做法**:所有股票任務(premarket、postopen、postclose、recover、crypto)與 `ensure_stops`、`sweep_orphan_stops` 一律透過 `execution/isolation.stock_only` 包裝券商,期權部位與委託對股票帳本不可見,且股票任務無法取消期權委託;`reconcile` 與股票 `RiskEngine` 另外再過濾一次,股票 `RiskEngine` 直接拒絕期權單。期權軌道用自己的 `options_*` 資料表與 `options_mismatches` 對帳。
- **仍存在的風險**:期權若持有到到期且價內,會被自動履約成股票部位,股票對帳會因此判定不一致並暫停新進場(保守、正確的反應,但會影響股票帳本);15:45 強制平倉是唯一的防線。`AlpacaBroker.cancel_all` 本身仍會取消整個帳戶的委託(股票任務不使用它)。

## 11. Grok 設計諮詢紀錄(2026-10-12)

採納(理由:與我們已有的證據一致,且全部先進影子帳戶驗證,不直接信任):
- 邊際主要在「高相對成交量 + 催化劑的個股」,指數上的機械式劇本證據弱。因此劇本加 RVOL 與 gap 門檻,grok_plan 只能在高 RVOL 標的下實單。
- 1 到 3 DTE 優於 0DTE 作為實單變體;0DTE 保留為影子變體,由資料決定。
- 不做的日子:宏觀數據日前後 30 分鐘為硬擋;VIX、開盤前相對成交量、前 15 分鐘無方向日、連續虧損日只做標記,累積樣本後再決定是否變成硬規則。
- 樣本:每個分類至少 80 到 150 筆才有統計意義;少於 50 筆幾乎無法區分運氣。

保留意見:
- Grok 引用的研究(Zarattini、Barbon、Aziz 2024 的 Stocks in Play ORB)是**股票**回測,未計入期權的價差、theta 與 IV 回落。尚未由我們獨立驗證,不能直接當作期權也有 edge。
- Grok 引用的 X 帳號績效沒有可驗證依據,不採用。
- 出場參數(-30%、ATR 追蹤)只當影子變體,不直接取代預設。理由:單一來源的經驗判斷,不是證據;一次只改一個東西。
- 信心分數只記錄、不用於部位大小,等校準後再說。

## 12. 操作手冊(2026-10-12 整合後,給 10-13 第一個交易日)

以下指令都在**主程式目錄**執行(`cd ~/Projects/stockmoney`),前提是 `opt/engine` 已合併進主線。先載入環境變數:`set -a; source .env; set +a`(推播與 Alpaca 金鑰都靠它)。

**1. 盤前劇本(07:30 到 09:20 ET 之間)**

```
.venv/bin/python -m stockmoney.desk.options.run ingest-plan ~/Downloads/plan.json
```

- 印出 `ACCEPTED <plan_id> for <日期>` 代表收下;09:20 ET 後才收到會加註「當天只進影子帳戶」。
- 印出 `REJECTED ...: <原因>` 代表整份拒收(並推播),例如時間窗早於 09:35、偏移不是 -04:00、代號小寫、超過 12 個劇本、多出欄位、bias 不是 long/short。改好再餵一次即可;同一份內容重送會顯示 `DUPLICATE`。
- 沒有劇本也沒關係,規則劇本照跑。

**2. 啟動盤中 session(09:20 到 16:00 ET)**

- 自動(使用者決定後才做):`.venv/bin/python scripts/desk_schedule.py --jobs options --install`,只安裝 options 這一個 launchd 工作,每個交易日 09:00 啟動,09:20 開始迴圈。不加 `--install` 只會把 plist 寫到 `data/desk/launchd/`。
- 手動:`nohup scripts/desk_job.sh options >/dev/null 2>&1 &`(輸出寫進 `data/desk/logs/<日期>.log`)。
- 只想跑影子、不下紙上單:啟動前 `export DESK_OPTIONS_ORDERS=off`。

**3. 確認 session 還活著**

- 09:20 會收到推播「期權軌道啟動」(含模式 paper/shadow、劇本狀態、HALT 狀態)。
- `pgrep -fl "options.run session"` 有一行;`lsof data/desk/options.lock` 顯示該程序持有鎖。
- `tail -f data/desk/logs/$(date +%F).log`:每輪約 20 秒;錯誤會以 `options <類別>: <訊息>` 出現。
- 資料庫:`sqlite3 data/desk/desk.sqlite "select max(updated_at) from options_setups where day=date('now','localtime')"`,時間應該在一兩分鐘內。新進場、出場、資料延遲、連續錯誤、收盤未平倉都會推播。

**4. 緊急煞車**

- `touch data/desk/HALT`:新進場全擋(影子照記),出場照常。注意這個檔案**股票主帳本也共用**,會一起擋股票新進場。解除:`rm data/desk/HALT`。
- 只擋期權新進場:`sqlite3 data/desk/desk.sqlite "insert into settings values('options_halt_new_entries','1') on conflict(key) do update set value='1'"`,解除把值改回 `0`。股票帳本若自動設了 `halt_new_entries=1`(對帳不一致),期權新進場也會被擋。
- 15:45 ET 一律強制平倉;收盤前 10 分鐘(15:50 ET)若仍有期權部位未平會先推播 CRITICAL,session 結束時再檢查一次券商期權部位,未平再推播 CRITICAL,請到 Alpaca 手動確認。
- 每一張賣單(出場)也經過 A 線風控:最多只能賣券商實際持有的口數(永遠不會變成賣出開倉);限價賣單若被價格帶擋下,改送市價賣單。風控擋下出場時不送單、每輪重試,並推播 CRITICAL 一次。

**4.1 提早收盤日(半天市)**

- 收盤時間優先讀 Alpaca 市場日曆,讀不到時用 `desk/calendar.py` 的 `EARLY_CLOSES`(已對 Alpaca 日曆核對:2026-11-27、2026-12-24、2027-11-26,皆 13:00 ET;兩者取較早)。
- 當天強制平倉 = min(15:45, 收盤前 15 分鐘),例如 13:00 收盤日為 12:45;A 線風控的 flat_by 用同一個時間;新進場的 180 分鐘時間停損不會超過它;距強制平倉不到 90 分鐘就不再送紙上實單(13:00 收盤日 11:15 ET 之後);session 迴圈在收盤時結束,未平倉警報在收盤前 10 分鐘(12:50)。

**4.2 前一日遺留部位與復原工作**

- 若 session 中途死掉(當機、休眠、斷電)沒在強制平倉前出場,隔天 session 的第一輪會推播 CRITICAL「前一日部位未平」,並在 09:30 ET 起以**市價**賣出平倉(進場單只成交一部分、剩下一直撤不掉的也一樣:到了強制平倉時間、時間停損到期或屬於前一日遺留時,先撤單,再賣出已成交的部分;撤單連續失敗會立刻推播)。前一日遺留的影子部位以最後已知買價補記出場。
- 選用的 launchd 工作 `options-recover`(開機 / 登入時執行一次,**不在預設工作內,不會自動安裝**):`.venv/bin/python scripts/desk_schedule.py --jobs options-recover --install`;手動:`scripts/desk_job.sh options-recover` 或 `.venv/bin/python -m stockmoney.desk.options.run recover`。它只做遺留部位清理:有 session 在跑(持有 options.lock)就直接結束;否則處理所有仍在 PENDING / ENTERED 的紙上實單(前一日的,以及當天沒有 session 在跑時的當天部位),市場開盤中就以市價賣出並最多輪詢 3 分鐘確認成交;非交易時段只推播,留給下個 session 09:30 處理。
- 只跑影子(`DESK_OPTIONS_ORDERS=off`)時,session 與復原工作用 ShadowGate:新進場一律拒絕,但遺留部位的賣單仍經 A 線風控(不會賣超過券商持有)並寫入期權帳本。
- **資料庫說持有、券商卻是 0**(手動平掉、到期或被履約時 Mac 沒開):遺留部位的賣單被風控擋下,且券商該合約持倉為 0、沒有任何掛單時,系統把這筆標記為已出場(`broker_flat_unreconciled`,日報顯示「損益未知」且不計入統計),在期權帳本寫一筆 `reconcile` 紀錄移除該部位,推播一次;這條路徑**絕不送單**。其他情況(例如當天的部位)可手動結案:先確認 Alpaca 上該合約已無持倉與掛單,再執行 `.venv/bin/python -m stockmoney.desk.options.run resolve <position_id>`(券商仍持有、仍有掛單或讀不到券商時一律拒絕;position_id 可從 `select position_id, occ, state from options_positions where mode='live' and state in ('PENDING','ENTERED')` 查)。
- **不要手動持有本軌道正在交易的同一個合約(同一個 OCC 代號)**:券商端無法分辨哪幾口是你的、哪幾口是本軌道的,本軌道的出場可能賣到你的口數,對帳也會混在一起。
- **手動在同一個 Alpaca 帳戶下期權單會擋住本軌道當天的新進場**:A 線風控比對期權帳本與券商持倉,出現帳本沒有的期權部位就視為不一致,拒絕所有買進(出場照常)。每天第一輪(以及復原工作)會比對券商期權部位與本軌道資料庫,發現本軌道不知道的部位會推播 CRITICAL「券商有未知期權部位」;本軌道**不會**自動賣出這種部位,請自行處理(該部位平掉後,新進場即恢復)。

**5. 盤後報告與重播**

```
.venv/bin/python -m stockmoney.desk.options.run report 2026-10-13
.venv/bin/python -m stockmoney.desk.options.run replay 2026-10-13 --feed iex
```

- session 收盤時已自動寫 `data/desk/options/reports/<日期>.md`;`report` 可以重寫並印出。內容(中文):資料限制說明、總覽、紙上實單逐筆、影子主變體(1-3dte、出場 A)依劇本 / 標的類別 / RVOL 分組(< 1.5、1.5 到 2、> 2)/ 盤勢標記、出場變體 A 對 B(DTE 固定)、DTE 變體 0dte 對 1-3dte(出場固定)、Grok 對規則(含同一 RVOL 條件)、被過濾掉的機會、異常。
- `replay` 要在收盤後 16 分鐘以上執行(SIP 延遲限制),第一次會下載快取到 `data/desk/options/replay_cache/<日期>/`;報告寫到 `replay_<日期>_iex.md`,開頭會寫明期權價格是合成模型。

**6. 10-12 重播結果(真實 IEX 1 分鐘 K、合成期權報價、A 線正式風控)**

- 正式參數:17 檔標的的 RVOL 都低於 1.5(SPY 開盤 5 分鐘 RVOL 0.49,已用原始資料手算核對),gap_go 與 orb15 一次都沒觸發(MU 跳空 -2.7% 但 RVOL5 只有 1.38),只有僅影子的 vwap_reclaim 觸發 12 次、進場 4 次(主變體合計 -486 美元,全部是推翻出場)。沒有紙上單,這是過濾設計的預期結果。
- 機制測試(把 RVOL 門檻降到 0.3,只為了讓下單路徑跑起來):3 筆模擬紙上單經過 A 線風控、入帳、出場、帳本歸零,沒有任何 client_order_id 重送。這次重播抓到並修正一個問題:同一輪第二筆進場曾因前一筆成交尚未入帳而被風控誤擋。
- session 端到端演練(模擬時鐘、模擬券商):09:20 開始、中途強殺後重啟不重送單、15:45 強制平倉、HALT 下出場照常、單一標的資料延遲時停止該標的進場、第二個 session 被檔案鎖拒絕,20 項檢查全過。

**7. 已知限制(第一天要有心理準備)**

- 期權報價只有 indicative feed(非 OPRA),影子成交按快照的買賣價計算;MFE/MAE 是每 20 秒輪詢的快照極值,不是盤中真實極值。
- 更正第 10 節的 A 線紀錄:期權 1 分鐘成交 K **歷史**查得到(10-12 收盤後實測整天 391 根),但查詢區間碰到最近 30 分鐘就回 HTTP 403(OPRA 未簽);盤中延遲 15 分鐘的查詢是否可用未驗證。系統盤中完全不依賴期權 K 線。
- 股票 1 分鐘 K 是 IEX,成交量只是 IEX 份額,所以所有量能條件都用同一資料源的比率;RVOL 需要前 20 個交易日(至少 10 個)的同一資料源歷史,抓不到就當作未知,該劇本只進影子。
- 合約過濾很可能常擋住實單:未平倉量 < 100(週三到期的個股合約常見)、價差 > 8%、單筆權利金上限 1,000 美元(MU 這類高價股買不起 0.45 到 0.60 delta)、週一時只有週五到期的標的沒有 1 到 3 DTE 合約。這些都會記在「被過濾掉的機會」。A 線風控在未平倉量未知時也會擋實單。
- VIX 前收沒有資料來源,一律記為無;宏觀事件只看劇本的 key_events 與設定檔清單。
- indicative 報價在盤中的時間戳新鮮度尚未在開盤時段實測;報價超過 30 秒視為過期,不會進場。
- 第一天的目標是確認流程與紀錄完整,不是看損益。

## 13. 劇本格式 v1.1 與 grok_stance(2026-10-12 使用者與 Grok 諮詢後的調整)

背景:Grok 表示拿不到盤前成交價與盤前成交量,所以改成由 Grok 提供方向與催化劑,gap、RVOL 與觸發由系統自己算。格式規則的唯一依據是驗證器 `src/stockmoney/desk/options/plan.py`;`GROK_SETUP.md` 與 `GROK_OPTIONS_PROMPT.md` 第二部分由測試 `tests/desk/options/test_opt_grok_docs.py` 核對(三個範例:有觸發、空劇本、只有方向判斷)。協定欄位仍是 `"1"`。

- 每個劇本必填:underlying、bias、playbook、thesis、catalyst、confidence、evidence_urls。選填:trigger、invalidation、target_underlying_pct、max_hold_minutes(省略時用 180 分鐘時間停損)、preferred_dte(省略為 any)、min_rvol、regime、daily_ref。寫了 trigger 就必須寫 invalidation。其餘限制不變,任何未知欄位仍整份拒收。
- key_events 的時間必須是 HH:MM(只接受 ASCII 數字)且在 09:00 到 15:30 ET 之間,否則整份拒收(盤前公布的低影響數據不能拿來擋進場)。
- **有 trigger 的劇本**:和以前一樣建立 grok_plan 劇本(自己的觸發、推翻價位、紙上資格規則不變)。
- **沒有 trigger 的劇本 = 方向判斷**:不建立自己的觸發。該標的加入當天觀察名單(照常經過流動性與 RVOL 過濾,每天上限 25 檔;盤中才收到時會補建該標的的規則劇本),該標的的每個規則劇本(gap_go、orb15、vwap_reclaim、gap_fade)在觸發當下記錄 `grok_stance`:方向相同為 `agree`、相反為 `disagree`、沒有方向判斷或判斷互相矛盾為 `none`;09:20 ET 後才收到的劇本照樣標記,另記 `grok_stance_late=1`(劇本本身只進影子)。
- `grok_stance` **只記錄、不影響**紙上資格與下單,下單仍完全由規則決定。日報新增「Grok 方向判斷 vs 規則劇本」一節:紙上實單、影子主變體與每個影子變體,依立場分組列出筆數、勝率、期望值與價差成本;任何一組少於 30 筆都會加上樣本數警告。判斷 Grok 有沒有加值,要看跨很多天累積的 agree 對 none 的差異,不看單日。
