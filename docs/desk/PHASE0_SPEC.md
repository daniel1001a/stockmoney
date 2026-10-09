# 第 0 階段規格:自動化地基

目標:系統每天自己跑,經 Alpaca 紙上帳戶下單(股票 + 加密貨幣),風控可否決任何單,每次執行都和券商對帳,異常會通知,Muse 橋樑可收發。**連續 5 個交易日零人工介入、對帳全對,才算過關。**

策略本身不在本階段範圍(沿用現有三個股票策略;加密貨幣與槓桿 ETF 策略是第 1 階段)。

## 共用契約(已寫好,worker 不得修改,要改先回報)

- `src/stockmoney/desk/execution/types.py`:OrderIntent / BrokerOrder / BrokerPosition / AccountSnapshot / RiskDecision
- `src/stockmoney/desk/execution/broker.py`:`Broker` Protocol + `FakeBroker`
- `src/stockmoney/desk/bridge/schema.py`:Muse 訊息 schema

## W1:Alpaca 券商介面 → `src/stockmoney/desk/execution/alpaca.py`

- `AlpacaBroker` 實作 `Broker`,使用已安裝的 `alpaca-py` 0.44.0。**所有 API 用法以 `.venv/lib/python3.12/site-packages/alpaca/` 原始碼為準,不可憑記憶。**
- 環境變數:`ALPACA_API_KEY`、`ALPACA_SECRET_KEY`、`ALPACA_ENV`(`paper` 預設)。`ALPACA_ENV=live` 時,必須同時有 `ALPACA_LIVE_ENABLED=I_UNDERSTAND_REAL_MONEY`,否則建構時直接 raise。這個值只能由使用者本人設定。
- 冪等:`submit` 先用 client_order_id 查詢,已存在就回傳既有單。
- 股票 + 加密貨幣(符號如 `BTC/USD`)。對應 enum 與 TIF 限制(股票:day/gtc/opg/ioc;加密貨幣:gtc/ioc)。不合法組合在送出前 raise,不要讓券商拒單。
- 網路錯誤:有限次數重試 + 退避;重試前先查 client_order_id,避免重複下單。
- 測試:mock `TradingClient`,不連網。涵蓋:enum 對應、冪等、實盤保護、加密貨幣 TIF 驗證、重試不重複下單。
- `scripts/alpaca_check.py`:有金鑰時印出帳戶、時鐘、持倉;**不下任何單**。

## W2:風控與安全層 → `src/stockmoney/desk/execution/risk.py`、`reconcile.py`、`ledger.py`

介面(orchestrator 會這樣呼叫):

```python
@dataclass
class Limits:
    capital_budget: float = 50_000      # 系統只把這麼多當作自己的錢,不管券商帳戶實際多少
    max_order_notional_pct: float = 0.25
    max_position_pct: float = 0.25
    max_gross_exposure_pct: float = 1.0
    max_crypto_exposure_pct: float = 0.30
    max_orders_per_run: int = 20
    price_band_equity: float = 0.10     # 委託參考價偏離前收超過這個比例就拒單
    price_band_crypto: float = 0.20
    dd_reduce: float = 0.20             # 系統淨值回撤 -> 新單規模減半
    dd_halt: float = 0.35               # -> 停止所有新進場(出場與停損照常)

class RiskEngine:
    def __init__(self, limits: Limits, con): ...
    def mode(self, snapshot: AccountSnapshot) -> tuple[str, str]: ...  # ("normal"|"reduced"|"halted", 理由)
    def check(self, intent: OrderIntent, snapshot: AccountSnapshot, ref_price: float,
              orders_this_run: int) -> RiskDecision: ...
```

- 規則:殺停開關(`data/desk/HALT` 檔案存在,或 settings `halt_new_entries=1`)時只允許降低曝險的單;回撤模式;單筆/單檔/總曝險/加密貨幣曝險上限;價格偏離;每次執行單數上限。**賣出或降低部位的單,除了價格檢查外一律放行。** 系統出問題時,要永遠能出場。
- 系統淨值 = capital_budget + 系統已實現與未實現損益(從 ledger 算),不是券商總淨值。高點記錄在 SQLite。
- 每個策略 sleeve 熔斷:回撤超過設定值時停用該策略的新進場。
- `ledger.py`:SQLite 表 `sleeve_positions(symbol PK, strategy, qty, avg_price, opened_at, stop)` 與 `fills`。規則:同一標的同時只能屬於一個策略。
- `reconcile.py`:`reconcile(ledger, snapshot) -> list[str]`,比對每檔數量(加密貨幣容許 1e-6)。有任何不一致,orchestrator 會進入 halted 並通知。
- 測試用 `FakeBroker`,每條規則至少一個「擋下」和一個「放行」的測試,外加「halted 時賣單仍放行」。

## W3:Muse 橋樑傳輸 → `src/stockmoney/desk/bridge/{mailbox,inbox,policy,outbox}.py`

- 設計見 `docs/desk/MUSE_BRIDGE.md`。只用標準庫 `imaplib`/`smtplib`/`email`。
- 環境變數:`DESK_MAIL_ADDRESS`、`DESK_MAIL_APP_PASSWORD`、`DESK_MAIL_IMAP`(預設 imap.gmail.com)、`DESK_MAIL_SMTP`(預設 smtp.gmail.com)、`MUSE_ADDRESS`、`MUSE_SHARED_TOKEN`。
- `inbox.poll(con, mailbox) -> int`:讀未讀信 → 主旨格式與 token 檢查 → 寄件人白名單 → schema 驗證 → 依 msg_id 去重 → 存原文 + 解析結果 + `received_at`。任何失敗:存進 `bridge_rejects`(含原因),不丟例外。處理過的信標記已讀。
- `policy.active_flags(con, now) -> BridgeFlags`:
  - `blocked: dict[ticker, reason]`:候選標的被 ALERT 或 RESEARCH_REPLY 標了風險 → 封鎖進場 3 天
  - `no_add: dict[ticker, reason]`:持倉遇 critical ALERT → 3 天內不加碼
  - `notify: list[str]`:需要推播給使用者的訊息
  - 權限規則:只能降低風險。程式碼裡不得有任何從 Muse 訊息產生買單或加大部位的路徑。
- `outbox.send(mailbox, msg: Outbound)`:主旨 `[SMD/<TYPE>/<msg_id>] <token>`,內文為 JSON。
- `mailbox.py`:`Mailbox` Protocol + `ImapSmtpMailbox` + 測試用 `FakeMailbox`。
- 測試不連網:token 錯、寄件人錯、多欄位、缺來源的 ALERT、重複 msg_id、IDEA 不產生任何旗標以外的效果。

## 主控(我自己做)→ `src/stockmoney/desk/run.py`、通知、排程

- 盤前任務(08:45 ET):讀 Muse 旗標 → 用前一天收盤算的訊號產生意向單 → 風控 → 以開盤市價單(OPG)送出
- 盤後任務(16:45 ET):成交回寫 ledger → 對帳 → 移動停損(券商端 GTC 停損單)→ 新訊號 → Muse WATCHLIST / RESEARCH_REQUEST / REPORT → 推播摘要
- Muse 輪詢:每 15 分鐘
- 通知:ntfy.sh(手機裝 ntfy app 訂閱私密 topic)
- 排程:macOS launchd,所有任務可重複執行(冪等)、可補跑

## 共通規則(每個 worker 都要遵守)

- 只改自己負責的檔案與對應測試;不動共用契約、不動 `CLAUDE.md`
- 不連任何實盤端點、不下任何單(包括紙上),測試全部離線
- 不憑記憶寫第三方 API,讀已安裝套件的原始碼
- 完成時:`.venv/bin/python -m pytest tests/desk -q` 全綠,並回報實際測試輸出
- commit 訊息結尾:`Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`
