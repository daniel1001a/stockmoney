# 兩台電腦資料同步(中文操作說明)

> 建立於 2026-07-15。背景:你有兩台自己的 Mac,會來回切換使用。
> **只有 A 機有 OpenClaw**(負責爬資料 + 跑 LLM 催化劑鏈),**B 機沒有**(只看盤)。
> 本文說明資料怎麼從 A 機流到 B 機,以及各自要跑什麼。

---

## 角色

| | A 機(有 OpenClaw) | B 機(沒有 OpenClaw) |
|---|---|---|
| 抓原始資料(新聞/OHLCV/期權/總經) | ✅ OpenClaw cron 自動 | ❌ 不抓,靠同步 |
| 跑 LLM 催化劑鏈 | ✅ | ❌ |
| 重算衍生表(features/預測/回測快取) | ✅ 自動 | ✅ **手動跑一個指令** |
| 服務 app(看盤) | ✅ | ✅ |

**資料流**:A 機 ingest → 匯出 Parquet → `git push origin main` → B 機 `git pull` → 本地重建 → 看盤。

> 為什麼 B 機要「重建」而不是直接收成品:同步只搬**原始表**(契約見
> `scripts/import_from_sync.py` 的 `TABLE_PRIMARY_KEYS`)。衍生表
> (`feature_store`/`daily_predictions`/`symbol_backtest_snapshot`)不進 git,
> 因為在本地用內建 LightGBM/logistic 重算很便宜,**不需要 OpenClaw、不需要付費 API**。

---

## A 機(OpenClaw 機)—— 全自動,不用管

以下 OpenClaw cron 已設好(`openclaw cron list` 可查):

- `stockmoney-news-ingest-hourly`:市場時段每小時抓新聞 → working DB。
- `stockmoney-options-snapshot` / `nightly-data-refresh`:盤後抓期權/OHLCV/總經 + 重算 features/預測(timeout 已調到 1200/1800s)。
- `stockmoney-refresh-live`(每小時 :30):把 working DB 原子發佈成 app 服務的 live DB。
- `stockmoney-export-for-sync`(`35 14-21 * * *`):匯出 Parquet 並 `git push origin main`,B 機才拿得到。

A 機看盤直接開 app 即可,資料本來就是最新。

---

## B 機(看盤機)—— 每次坐下來跑一個指令

```bash
cd ~/Projects/stockmoney
.venv/bin/python scripts/sync_pull_and_build.py --git-pull
```

這一個指令會依序做:`git pull` → 匯入同步的原始資料 → 重算 features →
重建預測/回測快取 → 原子發佈到 live DB。跑完直接開 app 就是最新。

之後照常啟動 app(`.claude/launch.json` 的 `fastapi-backend` + `frontend-vite`)。

---

## B 機的「第一次」設定(只做一次)

同步只搬「最近的增量」,不搬多年歷史。所以 B 機第一次要先有完整的歷史底庫:

1. 在 A 機把 `data/stockmoney.duckdb` 用 AirDrop / scp 複製到 B 機的
   `~/Projects/stockmoney/data/stockmoney.duckdb`(這是 gitignore 的大檔,不走 git)。
2. B 機 `git pull` 拿到最新程式碼。
3. 確認 B 機有 `.venv`(`uv sync` 或既有環境)與 `.env`(FRED 等金鑰;核心看盤
   其實用不到付費金鑰,但 `nightly_refresh` 若要自己抓資料才需要)。
4. 之後每次就只跑上面那一行 `sync_pull_and_build.py --git-pull`。

---

## 常見狀況

- **B 機新聞很舊**:代表 A 機的 `export-for-sync` 沒推上來,或 B 機忘了 `git pull`。
  先在 A 機看 `openclaw cron get <export-for-sync id>` 的 lastRunStatus;再在 B 機重跑上面指令。
- **push 衝突**:`export-for-sync` 會先 `git pull --rebase origin main` 再 push;
  若你在 B 機改了程式碼還沒 push,可能撞到,手動解一次即可。
- **絕不下單**:這整套只讀、只提示,永遠不接券商 API(見 `CLAUDE.md` 第 0 節)。
