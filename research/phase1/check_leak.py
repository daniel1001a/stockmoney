"""Look-ahead check: every signal value at day t must be identical whether it is
computed on the full history or on history truncated at t.

    cd research/phase1 && ../../.venv/bin/python check_leak.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import common as C
import crypto
import letf
import meanrev


def check(name: str, fn, series: pd.Series, n_points: int = 25, seed: int = 0) -> None:
    full = fn(series)
    rng = np.random.default_rng(seed)
    days = rng.choice(series.index[300:], n_points, replace=False)
    bad = 0
    for t in days:
        part = fn(series.loc[:t])
        for k in full:
            a, b = full[k].loc[t], part[k].loc[t]
            if not (a == b or (pd.isna(a) and pd.isna(b))):
                bad += 1
    print(f"{name}: {'OK' if bad == 0 else f'{bad} MISMATCHES'} ({n_points} random days x {len(full)} signals)")
    assert bad == 0


def mr_sig(c: pd.Series) -> dict[str, pd.Series]:
    return {k: meanrev.index_signal(c, e) for k, (e, _) in meanrev._setups(c).items()}


if __name__ == "__main__":
    core = C.fetch([], name="core")
    close = C.wide(core, "close")
    check("letf QQQ", letf.signals, close["QQQ"].dropna())
    check("letf SOXX", letf.signals, close["SOXX"].dropna())
    check("meanrev SPY", mr_sig, close["SPY"].dropna())
    check("crypto BTC", crypto.signals, close["BTC-USD"].dropna())
