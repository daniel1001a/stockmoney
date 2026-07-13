"""STOCKMONEY_DB env override for the default DB path (Wave A1).

Lets the whole app (API + every --db-defaulting script) point at the live DB
without code edits: `STOCKMONEY_DB=data/stockmoney_live.duckdb ...`.
"""
import importlib

import stockmoney.data.db as db_mod


def test_defaults_to_demo_db_when_env_unset(monkeypatch):
    monkeypatch.delenv("STOCKMONEY_DB", raising=False)
    reloaded = importlib.reload(db_mod)
    try:
        assert reloaded.DEFAULT_DB_PATH == "data/stockmoney.duckdb"
    finally:
        monkeypatch.delenv("STOCKMONEY_DB", raising=False)
        importlib.reload(db_mod)


def test_env_var_overrides_default(monkeypatch):
    monkeypatch.setenv("STOCKMONEY_DB", "data/stockmoney_live.duckdb")
    reloaded = importlib.reload(db_mod)
    try:
        assert reloaded.DEFAULT_DB_PATH == "data/stockmoney_live.duckdb"
    finally:
        monkeypatch.delenv("STOCKMONEY_DB", raising=False)
        importlib.reload(db_mod)


def test_api_layer_inherits_override(monkeypatch):
    # The API opens connections via stockmoney.api.db, which falls back to
    # data.db's DEFAULT_DB_PATH -- so the env override reaches request handlers.
    monkeypatch.setenv("STOCKMONEY_DB", "data/stockmoney_live.duckdb")
    importlib.reload(db_mod)
    import stockmoney.api.db as api_db

    reloaded_api = importlib.reload(api_db)
    try:
        assert reloaded_api.DEFAULT_DB_PATH == "data/stockmoney_live.duckdb"
    finally:
        monkeypatch.delenv("STOCKMONEY_DB", raising=False)
        importlib.reload(db_mod)
        importlib.reload(api_db)
