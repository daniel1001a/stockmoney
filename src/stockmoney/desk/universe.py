"""Tradable universe for the Trading Desk.

Three tiers, matching the 順勢 confirmation chain (market -> sector -> stock):
- MARKET: the benchmark whose trend gates all new risk.
- SECTORS: SPDR sector ETFs, ranked by relative strength to see where money flows.
- STOCKS: liquid large caps, each mapped to one sector ETF.

Caveat (documented, not hidden): this list is today's large caps, so any
backtest over it is survivorship-biased upward. The paper month is the honest
test; backtests only screen out ideas that fail even with that tailwind.
"""
from __future__ import annotations

MARKET = "SPY"

SECTORS: dict[str, str] = {
    "XLK": "科技",
    "SMH": "半導體",
    "XLC": "通訊",
    "XLY": "非必需消費",
    "XLP": "必需消費",
    "XLF": "金融",
    "XLV": "醫療",
    "XLI": "工業",
    "XLE": "能源",
    "XLB": "原物料",
    "XLU": "公用事業",
    "XLRE": "房地產",
}

STOCKS: dict[str, str] = {
    # semiconductors
    "NVDA": "SMH", "AVGO": "SMH", "AMD": "SMH", "TSM": "SMH", "MU": "SMH",
    "QCOM": "SMH", "AMAT": "SMH", "LRCX": "SMH", "KLAC": "SMH", "MRVL": "SMH",
    # software / hardware
    "AAPL": "XLK", "MSFT": "XLK", "ORCL": "XLK", "CRM": "XLK", "ADBE": "XLK",
    "NOW": "XLK", "PLTR": "XLK", "ANET": "XLK", "CRWD": "XLK", "PANW": "XLK",
    # communication
    "GOOGL": "XLC", "META": "XLC", "NFLX": "XLC", "TMUS": "XLC", "DIS": "XLC",
    # consumer discretionary
    "AMZN": "XLY", "TSLA": "XLY", "HD": "XLY", "MCD": "XLY", "NKE": "XLY",
    "BKNG": "XLY", "TJX": "XLY", "LOW": "XLY",
    # consumer staples
    "COST": "XLP", "WMT": "XLP", "PG": "XLP", "KO": "XLP", "PEP": "XLP",
    # financials
    "JPM": "XLF", "BAC": "XLF", "GS": "XLF", "MS": "XLF", "V": "XLF",
    "MA": "XLF", "AXP": "XLF", "BRK-B": "XLF", "SCHW": "XLF",
    # health care
    "LLY": "XLV", "UNH": "XLV", "JNJ": "XLV", "ABBV": "XLV", "MRK": "XLV",
    "ISRG": "XLV", "TMO": "XLV", "VRTX": "XLV",
    # industrials
    "GE": "XLI", "CAT": "XLI", "RTX": "XLI", "HON": "XLI", "UBER": "XLI",
    "DE": "XLI", "ETN": "XLI", "LMT": "XLI",
    # energy
    "XOM": "XLE", "CVX": "XLE", "COP": "XLE", "SLB": "XLE", "EOG": "XLE",
    # materials
    "LIN": "XLB", "FCX": "XLB", "NEM": "XLB", "SHW": "XLB",
    # utilities
    "NEE": "XLU", "VST": "XLU", "CEG": "XLU", "SO": "XLU",
    # real estate
    "PLD": "XLRE", "AMT": "XLRE", "EQIX": "XLRE",
}

ALL_SYMBOLS: list[str] = [MARKET, *SECTORS, *STOCKS]


def sector_of(symbol: str) -> str | None:
    return STOCKS.get(symbol)


def sector_name(etf: str | None) -> str:
    return SECTORS.get(etf or "", etf or "-")
