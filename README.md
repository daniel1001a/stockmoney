# stockmoney

Trend-following trading desk with human-in-the-loop paper trading.
See `docs/desk/OPERATING_PLAN.md`.

```bash
uv sync
.venv/bin/python scripts/desk_daily.py      # nightly run, after 16:30 ET
.venv/bin/streamlit run app/desk_app.py     # console
```
