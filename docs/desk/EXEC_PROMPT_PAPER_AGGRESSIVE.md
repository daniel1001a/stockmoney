# 執行用 Prompt:紙上積極期(貼進你本機的 Claude Code,在有 `data/desk/desk.sqlite` 的專案目錄)

> 2026-10-21。對應 OPERATING_PLAN 第 10 節。實盤閘門不變;Claude 不下真錢單、不碰實盤金鑰。

---

你是這個 repo 的唯一程式執行者。先讀 `CLAUDE.md`、`docs/desk/OPERATING_PLAN.md`(第 10 節)、`docs/desk/EDGE_PLAN.md`。目標:紙上帳戶要大膽,快速累積樣本,找出重複的 pattern 與我自己的交易方式,同時不破壞風控與驗證紀律。省 token:資料處理用腳本,只在「規劃/假設審查」用高階模型,其餘用 Sonnet。

## 步驟 1:診斷(先不改任何東西)
1. 用 `.venv/bin/python` 讀 `data/desk/desk.sqlite`,輸出:Alpaca 帳戶 vs `shadow:combined` vs `shadow:combined_t35` vs SPY 的報酬、最大回撤、每筆期望值(95% 區間)。
2. 跑 `scripts/desk_learn.py h1-report --no-record` 與 `h2-report --no-record`;統計驗屍卡症狀(S1-S9)的次數與總虧損;列出被研究否決或 Muse/Grok 擋掉的單與其後走勢。
3. 用一張表回報:賺錢來源、虧錢來源、被洗出場比例、執行滑價。**只報事實,不下結論。**

## 步驟 2:啟用積極檔(紙上)
1. 確認 `.env` 的 `ALPACA_ENV=paper`,然後加 `DESK_PROFILE=paper_aggressive`(檢查排程 launchd 讀得到這個變數)。
2. 跑 `.venv/bin/python -m pytest tests/desk -q`,全過再繼續。
3. 在 `param_changes` 記一筆:啟用時間、理由(增加樣本)、對照組(shadow:combined,標準檔)。

## 步驟 3:找 pattern(每週一次,用高階模型規劃)
1. 讀本週驗屍卡與日誌,提出最多 3 個假設,格式:「我認為 X,因為 Y;驗證方式 Z;失敗條件 W」。
2. 每個假設走 回測 → 拆兩段 → 隨機對照 → 影子帳戶 ≥4 週 → 我核准。一次只改一件事,不因單筆虧損改規則。
3. 優先序:H1(移動停損 3.5 ATR)、H2(財報前不進場)、再來是「持倉中途大盤翻轉的處理」。

## 步驟 4:盤中掃描(即時化第一步,先做影子)
1. 新增 `desk/intraday.py`:每 5-15 分鐘用 Alpaca 即時資料重算突破/回檔訊號,**先只寫入影子表,不下單**。
2. 與日線訊號比較:盤中提早進場是否有優勢(進場價差、之後報酬)。累積 ≥30 筆再評估是否接進下單路徑,下單仍必須過 `RiskEngine`。
3. 禁止:串流、日內槓桿、選擇權,在盤中影子證明有效前都不做。

## 步驟 5:Grok + Claude 整合
1. 把 `docs/desk/GROK_PROMPT.md` 的 Grok 輸出接進 bridge inbox(來源標 grok),權限=只能降低風險。
2. 在驗屍卡加欄位:Grok 與 Muse 是否獨立指向同標的;每週統計「雙來源一致」時的結果差異。
3. 任何權限擴大(讓共識微調部位)只寫成提案,等我明確核准並改 `MUSE_BRIDGE.md`。

## 輸出要求
每步完成後給:跑過的指令、測試輸出、改動檔案、下一步需要我決定的事。不要自稱成功而沒有證據。commit 後推到現有開發分支,不要開 PR 除非我說。
