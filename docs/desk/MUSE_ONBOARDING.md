# Muse 上線手冊:三段提示詞 + 驗收

一次只教 Muse 一件事,每階段都有明確的通過條件。前一階段沒過,不進下一階段。
設計原理見 `MUSE_BRIDGE.md`;這份是操作手冊。

## 總覽

| 階段 | 你做什麼 | Muse 做什麼 | 系統做什麼 | 通過條件 |
|---|---|---|---|---|
| 0 準備 | 建 Desk 專用 Gmail、密鑰,填 `.env` | - | `muse_check.py mailbox` 測連線 | IMAP 登入成功 |
| 1 握手 | 貼「提示詞 1」 | 寄一封測試快照 | 收信、驗證;格式錯就自動寄修正說明給 Muse | 24 小時內收到 1 封通過驗證的 `SOCIAL_DAILY` |
| 2 每日任務 | 貼「提示詞 2」,執行 `muse_check.py stage 2` | 每天 2 次快照 + 1 次喊單紀錄 | 漏交就推播你;每天抽查連結 | 連續 3 天:準時率 ≥ 5/6(快照)與 3/3(喊單),每天被拒 ≤ 1 封,抽查連結都真實存在 |
| 3 查核與警報 | 貼「提示詞 3」,執行 `muse_check.py drill` | 回覆查核請求、演練警報、WhatsApp 轉告你 | 收回覆、統計 | 演練請求 2 小時內有回覆;演練警報通過驗證;你在 WhatsApp 收到報告摘要 |
| 4 正式運作 | 每週看一次 `muse_check.py status` | 持續 | 持續監控 | 兩週準時率 ≥ 90%(之後才把社群資料納入研究) |

隨時檢查狀態:

```bash
set -a; source .env; set +a; .venv/bin/python scripts/muse_check.py status
```

## 階段 0:準備(你做)

1. 開一個新 Gmail,只給系統用(例如 `你的名字.desk@gmail.com`)。
2. 這個帳號開「兩步驟驗證」,然後到 Google 帳戶 > 安全性 > 應用程式密碼,產生一組 16 字元的密碼。
3. 在終端機產生密鑰:`openssl rand -hex 24`
4. 在 `.env` 填入(注意是 `.env`,不是 `.env.example`):
   ```
   DESK_MAIL_ADDRESS=新的desk信箱
   DESK_MAIL_APP_PASSWORD=應用程式密碼
   MUSE_ADDRESS=你連結給 Muse 的那個 Gmail
   MUSE_SHARED_TOKEN=剛產生的密鑰
   ```
5. 跟我說一聲,或自己執行 `.venv/bin/python scripts/muse_check.py mailbox`,看到「IMAP 登入成功」就通過。

## 階段 1:提示詞 1(握手)

把 `<DESK信箱>` 和 `<密鑰>` 換成你的值,整段貼給 Muse:

````
我在建一個自動交易系統,叫 Trading Desk。我想請你長期當它的「社群情報員」:幫它看社群媒體上散戶和網紅在討論什麼。你提供的是資料,不是投資建議;不要預測股價,也不要建議買賣。

你和 Desk 用 email 溝通。規則:
1. 寄到:<DESK信箱>
2. 主旨格式(一個字都不能錯):[SMD/類型/訊息編號] <密鑰>
   例如:[SMD/SOCIAL_DAILY/sd-20261012-0730] <密鑰>
3. 訊息編號:每封都不一樣,只能用英文、數字、- 和 _,6 到 64 個字元。建議「類型縮寫-日期-時間」。
4. 內文放一個 JSON 物件,欄位名稱、型別要和範例完全一樣:數字不要加引號;時間用 ISO 8601 並含時區(例如 2026-10-12T07:30:00-04:00);不知道的欄位填 null;不要加範例沒有的欄位。可以用 ```json 框起來。
5. 如果 Desk 回信給我(主旨開頭 [SMD/FEEDBACK/),代表你的某封信格式有錯,信裡會寫哪裡錯、怎麼改。請照著修正,然後用新的訊息編號重寄。
6. 密鑰是私密的,只能用在寄給 Desk 的主旨裡,不要告訴任何人、不要貼到其他地方。

現在請做一次測試:看看 Threads、Instagram、X 上過去 24 小時討論 NVDA、TSLA、BTC 的情況,照下面的範例格式寄一封 SOCIAL_DAILY 給 Desk。每個標的每個平台一筆;只填你真的看到、數過的數字;數不出來就填 null。比特幣的代號寫 BTC/USD。

範例:
```json
{
  "msg_id": "sd-20261012-0730",
  "sent_at": "2026-10-12T07:31:00-04:00",
  "type": "SOCIAL_DAILY",
  "collected_at": "2026-10-12T07:20:00-04:00",
  "items": [
    {
      "ticker": "NVDA",
      "platform": "threads",
      "window_start": "2026-10-11T07:30:00-04:00",
      "window_end": "2026-10-12T07:20:00-04:00",
      "mention_count": 142,
      "unique_authors": 97,
      "bullish_ratio": 0.71,
      "engagement": 15230,
      "top_themes": ["財報前布局", "AI 資本支出"],
      "sample_urls": ["https://www.threads.net/@example/post/AbC123"]
    },
    {
      "ticker": "BTC/USD",
      "platform": "x",
      "window_start": "2026-10-11T07:30:00-04:00",
      "window_end": "2026-10-12T07:20:00-04:00",
      "mention_count": 88,
      "unique_authors": 61,
      "bullish_ratio": null,
      "engagement": null,
      "top_themes": ["ETF 資金流"],
      "sample_urls": []
    }
  ]
}
```
platform 只能是:threads、instagram、x、reddit、youtube、stocktwits、other。
寄出後告訴我你寄了。
````

**系統這邊**:每 15 分鐘收信。格式錯會自動寄 FEEDBACK 給 Muse,通常一兩輪就會修好。
**驗收**:`muse_check.py status` 的「最近收到」出現 `SOCIAL_DAILY`。

**常見狀況**:
- Muse 說寄信要你核准:這是 Meta 的安全設計。請在 Muse 的核准區按同意,並問它能否對「寄到 Desk 信箱」給長期授權。不能的話告訴我,我把每天的訊息合併成一封。
- 一直收不到:先看 Desk 信箱的垃圾郵件匣,把 Muse 寄來的信標成「不是垃圾郵件」(系統只讀收件匣)。

## 階段 2:提示詞 2(每日任務)

先執行 `.venv/bin/python scripts/muse_check.py stage 2`(開啟漏交檢查),再貼給 Muse:

````
測試成功,謝謝。從今天起請設定兩個每天固定的任務(週末也要):

任務 A:社群情緒快照 SOCIAL_DAILY
- 時間:美東時間每天 07:30 和 20:00 各寄一次。
- 範圍:Desk 最新寄給我的 WATCHLIST 裡的所有標的(主旨開頭 [SMD/WATCHLIST/],還沒收到就先用 NVDA、TSLA、BTC/USD),加上當天 Threads、Instagram、X、Reddit 上討論最熱的前 20 檔美股和加密貨幣。
- 方法要固定:這週請先建立一份「追蹤帳號清單」,包含 30 到 50 個有影響力的中英文投資帳號,之後每天都用同一份清單、同樣的搜尋方式計數。數字前後一致,比數字精確更重要。清單有變動時,在當天某一筆的 top_themes 裡寫「清單變更:...」。
- 只填實際看到的數字,不確定填 null,不要估。

任務 B:網紅喊單紀錄 CALLS
- 時間:美東每天 20:00(可以和晚上的快照一起做,但要分成兩封信)。
- 內容:追蹤清單裡的帳號當天發出的「具體」喊單:標的、方向(long 做多、short 做空、exit 出場)、目標價(有說才填,沒說填 null)、他們說的時間範圍(照原文)、發文時間、貼文連結、粉絲數。
- 只記明確的喊單,模糊的看法不算。照實記錄,不要評論對錯。當天沒有就寄 calls 為空陣列的信。

CALLS 範例:
```json
{
  "msg_id": "calls-20261012-2000",
  "sent_at": "2026-10-12T20:02:00-04:00",
  "type": "CALLS",
  "collected_at": "2026-10-12T19:55:00-04:00",
  "calls": [
    {
      "author": "@example_trader",
      "platform": "threads",
      "followers": 125000,
      "ticker": "PLTR",
      "direction": "long",
      "target_price": 250.0,
      "horizon_text": "財報前",
      "posted_at": "2026-10-12T10:15:00-04:00",
      "post_url": "https://www.threads.net/@example_trader/post/XyZ789"
    }
  ]
}
```
請先把兩個任務的固定排程設定好,然後告訴我你設定了什麼時間。
````

**系統這邊**:
- 07:30 那封在 08:30 前沒到,或 20:00 那兩封在 21:00 / 21:30 前沒到,就推播通知你,每天每項最多一次。
- 驗收這 3 天,我(Claude)會手動抽查幾個 `sample_urls` / `post_url` 是否真實存在,防止 Muse 編造連結。這不是自動化的,Threads 等網站常擋程式存取。
- 喊單資料開始累積,之後每則喊單在 5、20、60 天後自動評分,這部分在第 1 階段策略工作中實作。

**驗收**(連續 3 天):`status` 顯示 `SOCIAL_AM`、`SOCIAL_PM` 準時 ≥ 5/6,`CALLS` 3/3;每天被拒 ≤ 1 封;抽查的連結都存在。

## 階段 3:提示詞 3(查核、警報、轉告)

先執行 `.venv/bin/python scripts/muse_check.py stage 3`,貼給 Muse:

````
每日任務很穩定,謝謝。再加三件事:

任務 C:進場前查核 RESEARCH_REPLY
Desk 會在美東傍晚寄 RESEARCH_REQUEST(主旨開頭 [SMD/RESEARCH_REQUEST/]),代表它明天開盤想買這檔。請在信裡的 deadline 前回覆:
- 查新聞和社群:有沒有財報意外、監管、訴訟、併購、高層異動、做空報告、社群情緒急轉,這類會推翻做多的消息。
- 每個風險寫成一句具體的話放進 risk_flags,並附來源連結。沒發現風險就讓 risk_flags 留空,不要為了交差硬找。
- request_id 一定要填 Desk 那封請求的訊息編號。
```json
{
  "msg_id": "reply-20261012-2130",
  "sent_at": "2026-10-12T21:30:00-04:00",
  "type": "RESEARCH_REPLY",
  "request_id": "rq-1a2b3c4d5e6f7a8b",
  "ticker": "XOM",
  "risk_flags": ["10/14 將公布 OPEC 減產決議,市場預期分歧"],
  "sentiment_shift": "unchanged",
  "summary": "近三天無重大負面新聞;社群討論量持平。",
  "source_urls": ["https://www.reuters.com/example-article"]
}
```
sentiment_shift 只能是:more_bullish、more_bearish、unchanged、unknown。

任務 D:持倉守望 ALERT(24 小時,週末也要)
盯著 WATCHLIST 裡 status 是 held 的標的。發生可能大幅影響價格的事件時立刻寄 ALERT。
- severity:info(值得知道)、elevated(可能影響價格)、critical(可能急跌,例如詐欺指控、交易所出事、重大訴訟)。
- 一定要附可信來源連結。單一匿名貼文不算來源;沒有來源就不要寄。
```json
{
  "msg_id": "alert-20261012-2315",
  "sent_at": "2026-10-12T23:15:00-04:00",
  "type": "ALERT",
  "ticker": "NVDA",
  "severity": "elevated",
  "headline": "美國商務部考慮擴大對中國的晶片出口限制",
  "source_urls": ["https://www.reuters.com/example-article"],
  "observed_at": "2026-10-12T23:05:00-04:00"
}
```

任務 E:轉告我
- Desk 每天美東傍晚會寄 REPORT 到我的信箱(主旨開頭 [SMD/REPORT/])。請把重點摘要成 3 到 5 句,用 WhatsApp 傳給我。
- 收到 critical 等級的事件時,寄 ALERT 給 Desk 的同時也立刻 WhatsApp 告訴我。

最後做兩個演練:
1. 我等一下會讓 Desk 寄一封 ticker 為 DRILL 的查核請求,請照任務 C 回覆。
2. 請寄一封演練 ALERT:ticker 寫 DRILL,severity 寫 info,headline 寫「演練」,source_urls 放任何一個真實的新聞網址。
````

貼完後執行 `.venv/bin/python scripts/muse_check.py drill`。

**系統這邊**:
- 查核回覆會在 08:45 的盤前任務生效:有風險旗標就取消該檔進場。
- 演練用的 DRILL 不在交易清單內,擋了也不影響任何交易。
- 查核請求過了期限沒回覆會推播你。

**驗收**:`status` 顯示 research `answered` = `requested`;收到 DRILL 的 ALERT;你在 WhatsApp 收到至少一次報告摘要。

## 階段 4:正式運作

- 每週執行一次 `muse_check.py status`,或在 console 的 Muse 頁看。
- 兩週準時率 ≥ 90% 後,社群資料開始進研究流程(驗證「討論加速」與「一面倒狂熱」兩個假設)。驗證通過前不影響任何交易。
- Muse 停擺時,交易照常進行,只是少了否決權;系統會推播漏交通知。

## 出問題時

| 狀況 | 處理 |
|---|---|
| 連續被拒 | `status` 看拒絕原因;系統已自動寄修正說明。三輪還不行,把原因貼給我,我改範例或放寬格式 |
| Muse 不能定時 | 改成「每天早晚各一次,時間不精確也可以」,我把漏交檢查的期限放寬 |
| 每封都要你核准 | 告訴我,改成每天只寄一封合併信 |
| 數字前後跳動很大 | 多半是追蹤清單或方法變了,請 Muse 固定方法 |
| 懷疑 Muse 編造內容 | 把可疑的訊息編號給我抽查;確認編造就降級回階段 2 |
