"""Read-only owner-setting metadata for the research registry.

No broker is constructed and this module exposes no order operations.
"""
import os

from stockmoney.desk.execution.alpaca import LIVE_CONFIRMATION
from stockmoney.desk.execution.risk import Limits


def risk_default(param: str):
    value = getattr(Limits(), param)
    return str(value) if param == "halt_file" else value


def switch_state(key: str):
    if key == "DESK_OPTIONS_ORDERS":
        return "off" if os.environ.get(key, "paper").strip().lower() == "off" else "paper"
    if key == "ALPACA_LIVE_ENABLED":
        return os.environ.get(key) == LIVE_CONFIRMATION
    if key == "ALPACA_ENV":
        return (os.environ.get(key) or "paper").strip().lower()
    raise KeyError(key)
