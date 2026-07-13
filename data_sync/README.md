# `data_sync/` — cross-machine data handshake (Agent 3 → Agent 4)

The `.duckdb` files are gitignored on both machines, so committed **Parquet**
files under `exports/` are the only channel by which the data Agent 3 (OpenClaw)
captures reaches the dev machine. Agent 4 imports them with
`scripts/import_from_sync.py` into the local live DB (`data/stockmoney_live.duckdb`).

## Export layout (Agent 3 writes; Agent 4 reads)

```
data_sync/exports/<table>/<anything>.parquet
```

One subdirectory per table. File names are free-form (e.g. `2026-07-12.parquet`);
the importer keys idempotency on file **content SHA-256** + each table's primary
key, not on the name.

## Tables and primary keys (must match the migration definitions)

The importer's `TABLE_PRIMARY_KEYS` must stay a **superset** of what
`scripts/export_for_sync.py` ships, or exported rows silently never reach the
live DB.

| table                      | primary key                                                    |
|----------------------------|----------------------------------------------------------------|
| `iv_surface_daily`         | symbol, trade_date, expiry_date, delta_bucket, ingested_at     |
| `put_call_ratio_daily`     | symbol, trade_date, ingested_at                                |
| `options_derived_daily`    | symbol, trade_date, metric_name, method_version, ingested_at   |
| `catalyst_signals`         | signal_id                                                      |
| `news_items`               | item_id                                                        |
| `news_articles_raw`        | article_id, ingested_at                                       |
| `scan_classifications`     | item_id, item_type                                            |
| `ohlcv_daily`              | symbol, trade_date, ingested_at                               |
| `vix_term_structure_daily` | trade_date, tenor_days, ingested_at                           |
| `macro_series_daily`       | series_id, observation_date, vintage_date                     |
| `alt_social_hourly`        | symbol, platform, hour_bucket, ingested_at                    |
| `watchlist_candidates`     | candidate_id                                                  |
| `ingestion_runs`           | run_id                                                        |

## Rules for the exporter

- **Preserve `ingested_at` / `available_at` verbatim** — they are the
  actually-available (look-ahead) timestamps and, for the options tables, part
  of the PK. Never regenerate them at import time.
- Include every `NOT NULL` column for the table; extra/stray columns are
  rejected loudly by the importer.
- Re-exporting a file with rows appended is fine: the new checksum makes the
  importer re-read it, and the PK anti-join inserts only the genuinely new rows.

The importer is idempotent and safe to re-run / schedule (Agent 3 owns the
scheduling; see `NEXT_AGENT_PLAN.md` trust boundary).
