# Muse 橋樑設計

Muse 是 24 小時在線的外部 AI agent,擅長蒐集社群與網路資訊。Trading Desk 是只在排程時間醒來的系統。橋樑讓兩邊互補:**Muse 當 24 小時的眼睛和耳朵,Desk 當有紀律的大腦和手。**

## 1. 通道:Email

選 email 的原因:Muse 已經能收發;非同步,任何一邊離線都不會掉訊息;每則訊息都有存檔可稽核;不需要你的記事本或任何共享檔案系統。

- **專用信箱**:建議另開一個只給系統用的 Gmail,例如 `xxx.desk@gmail.com`。不要用你的個人信箱,因為系統會用程式讀信,權限要隔離。帳戶要由你建立,並產生「應用程式密碼」放進 `.env`
- **Desk 讀信**:IMAP,每 15 分鐘輪詢一次
- **Desk 寄信**:SMTP,寄給 Muse 的地址
- **格式**:主旨 `[SMD/<TYPE>/<msg_id>]`,內文就是一個 JSON 物件(schema 見 `src/stockmoney/desk/bridge/schema.py`)

## 2. 訊息類型

**Muse 寄給 Desk**

| 類型 | 何時 | 用途 |
|---|---|---|
| `SOCIAL_DAILY` | 每天美東 07:30(盤前)與 20:00(盤後) | 社群情緒:提及數、作者數、看多比例、互動量、主題 |
| `ALERT` | 隨時,24/7 | 監控名單上的標的出現重大事件,必須附來源連結 |
| `RESEARCH_REPLY` | 回覆 Desk 的請求 | 針對某檔的風險檢查結果 |
| `IDEA` | 隨時 | Muse 發現的機會,只進孵化器紀錄 |

**Desk 寄給 Muse**

| 類型 | 何時 | 用途 |
|---|---|---|
| `WATCHLIST` | 每天盤後 | 告訴 Muse 未來 24 小時要盯哪些標的(持倉、候選、觀察) |
| `RESEARCH_REQUEST` | 出現新訊號時 | 「這檔明天要進場,幫我查有沒有消息面風險」,附截止時間 |
| `REPORT` | 每天盤後 | 給你看的每日報告,Muse 可以轉給你 |

一個典型的一天:

```
16:45  Desk 盤後執行 -> 產生新訊號 NVDA、XOM
       -> 寄 WATCHLIST(持倉 + 候選)給 Muse
       -> 寄 RESEARCH_REQUEST(NVDA、XOM,截止 08:30)
       -> 寄 REPORT 給 Muse 轉給你
晚上    Muse 24 小時盯盤;某持倉深夜出事 -> 寄 ALERT
07:30  Muse 寄 SOCIAL_DAILY + RESEARCH_REPLY
08:45  Desk 盤前執行 -> 讀所有回覆與警報 -> 套用權限規則 -> 送出開盤單
```

## 3. 權限規則(最重要)

Muse 的輸入**只能降低風險,不能增加風險**:

| Muse 的輸入 | Desk 自動執行的動作 | 不會做的事 |
|---|---|---|
| `ALERT` severity=critical,標的為持倉 | 立刻通知你;該標的暫停加碼 3 天 | 不會自動賣出 |
| `ALERT` / `RESEARCH_REPLY` 有風險旗標,標的為候選 | 取消這次進場,記錄原因 | 不會改其他標的 |
| `SOCIAL_DAILY` | 存進特徵庫,先累積 | 驗證通過前不影響任何交易 |
| `IDEA` | 寫進孵化器,影子帳戶追蹤 | 不會用真錢或紙上主帳戶交易 |

為什麼 critical 警報也不自動賣?因為只憑一個外部來源的文字就自動賣出,等於讓任何能影響 Muse 的人(例如在社群植入假消息)控制你的部位。出場交給價格本身(停損)來確認:消息是真的,價格就會反映,停損會處理。

社群情緒只有在前瞻紀錄證明有預測力之後(至少 2-3 個月),才能升級成策略的輸入特徵。升級要走正常的策略驗證流程。

## 4. 安全

- **寄件人白名單**:只接受 Muse 的寄件地址
- **共享密鑰**:每則訊息的主旨要帶一組只有 Muse 和 Desk 知道的 token(寄件人地址可以偽造,token 不行)
- **嚴格 schema**:欄位多一個、少一個、格式錯,整則丟棄並記錄,不猜意思
- **文字只給人看**:`headline`、`summary`、`thesis` 這類自由文字只顯示給你,不會被當作指令,也不會被丟給有工具權限的 AI
- **時間戳**:同時記錄 Muse 的 `sent_at`/`collected_at` 和 Desk 的 `received_at`;回測只用 `received_at`,防止偷看未來

## 5. 給 Muse 的設定說明(可直接貼給 Muse)

> 你是 Trading Desk 的資訊夥伴。請透過 email 與 `<desk 信箱>` 溝通,主旨格式 `[SMD/<TYPE>/<msg_id>] <token>`,內文只放一個符合 schema 的 JSON 物件,不要加其他文字。
> 1. 每天美東 07:30 和 20:00 寄 `SOCIAL_DAILY`:Threads、Instagram 等平台上,Desk 最新 `WATCHLIST` 內標的與當日熱門美股/加密貨幣的提及數、不重複作者數、看多比例、互動量、前幾大主題。`collected_at` 填你實際抓取的時間。數字比結論重要,沒有把握的欄位填 null,不要估。
> 2. 24 小時監控 `WATCHLIST` 內 status=held 的標的。發生可能影響股價的重大事件(財報意外、監管、訴訟、併購、高層異動、駭客、交易所問題等)時寄 `ALERT`,必須附來源連結。沒有來源就不要寄。
> 3. 收到 `RESEARCH_REQUEST` 時,在 deadline 前回覆 `RESEARCH_REPLY`,列出具體風險旗標與來源。
> 4. 發現有趣的機會可寄 `IDEA`,附論點與來源。
> 5. 收到 `REPORT` 時,轉給 Daniel。
> 完整 schema 見附件。

## 6. 你需要準備的

1. 建立專用 Gmail,開兩步驟驗證,產生應用程式密碼
2. 告訴我 Muse 寄信用的地址
3. 把第 5 節貼給 Muse,連同 schema
