"""Shared GROK_DAILY builders for the Grok tests."""
from __future__ import annotations

import copy
import json

TOKEN = "s3cret-shared-token-abcdef"
SENDER = "owner@example.com"
TS = "2026-10-12T07:30:00-04:00"


def grok_daily(msg_id: str = "grok-20261012-0730", sent_at: str = TS,
               collected_at: str = "2026-10-12T07:05:00-04:00") -> dict:
    return {
        "protocol": "1", "msg_id": msg_id, "sent_at": sent_at, "type": "GROK_DAILY", "collected_at": collected_at,
        "events": [{"ticker": "NVDA", "headline": "New export curbs reported", "severity": "critical",
                    "confirmed": True, "first_seen": "2026-10-12T06:10:00-04:00",
                    "urls": ["https://www.reuters.com/technology/nvidia-curbs-2026-10-12/"]}],
        "chains": [{"trigger": "Export curbs on AI GPUs", "urls": ["https://x.com/a/status/1"],
                    "steps": ["China datacenter sales fall", "Suppliers of China SKUs see cuts"],
                    "impacts": [{"ticker": "NVDA", "direction": "down", "horizon_days": 5},
                                {"ticker": "BTC/USD", "direction": "up", "horizon_days": 20}],
                    "checkpoints": [{"what": "Final rule text", "date": "2026-10-20"}],
                    "invalidations": [{"kind": "close_above", "symbol": "NVDA", "level": 150.0,
                                       "by_date": "2026-10-30"}],
                    "confidence": 0.55}],
        "calls": [{"author": "@trader_abc", "platform": "x", "followers": None, "ticker": "TSLA",
                   "direction": "long", "target_price": 300.0, "horizon_text": "by earnings",
                   "posted_at": "2026-10-11T21:14:00-04:00", "post_url": "https://x.com/trader_abc/status/2"}],
        "attention": [{"ticker": "PLTR", "trend": "rising", "why": "Contract news"}],
        "tracked_accounts": ["@trader_abc", "@zh_trader"],
        "notes": "",
    }


def worst_case() -> dict:
    """Every list full, every string at its limit in CJK (3 bytes each in UTF-8)."""
    c, ts, tk = "中", TS, "BTC/USD"
    url = "https://x.com/" + "a" * (250 - len("https://x.com/"))
    return {
        "protocol": "1", "msg_id": "grok-" + "a" * 59, "sent_at": ts, "type": "GROK_DAILY", "collected_at": ts,
        "events": [{"ticker": tk, "headline": c * 160, "severity": "critical", "confirmed": False,
                    "first_seen": ts, "urls": [url] * 3, "basis": "inference"}] * 25,
        "chains": [{"trigger": c * 200, "urls": [url] * 3, "steps": [c * 120] * 6,
                    "impacts": [{"ticker": t, "direction": "down", "horizon_days": 60, "order": 2}
                                for t in (tk, "BRK-B", "GOOGL", "TQQQ", "NVDA", "QQQ")],  # one per ticker
                    "checkpoints": [{"what": c * 100, "date": "2026-10-30"}] * 4,
                    "invalidations": [{"kind": "data_not_by", "description": c * 100, "by_date": "2026-10-30"}] * 3,
                    "confidence": 0.123456789}] * 6,
        "calls": [{"author": "@" + "a" * 15, "platform": "x", "followers": 123456789, "ticker": tk,
                   "direction": "short", "target_price": 123456.789012, "horizon_text": c * 60,
                   "posted_at": ts, "post_url": url}] * 50,
        "attention": [{"ticker": tk, "trend": "falling", "why": c * 120}] * 20,
        "tracked_accounts": ["@" + format(i, "015d") for i in range(60)],
        "notes": c * 2000,
        "link_check": {"total": 1000, "passed": 1000, "removed": 1000},
        "market_risk": {"level": "critical", "summary": c * 300, "source_urls": [url] * 5},
    }


def subject(payload: dict, token: str = TOKEN) -> str:
    return f"[SMD/{payload['type']}/{payload['msg_id']}] {token}"


def deliver(box, payload: dict, *, sender: str = SENDER, body: str | None = None) -> str:
    return box.inject(sender, subject(payload), body if body is not None else json.dumps(payload, ensure_ascii=False))


def patched(path: str, value, base: dict | None = None) -> dict:
    """Copy of a valid message with one dotted path replaced ('chains.0.confidence')."""
    d = copy.deepcopy(base or grok_daily())
    *head, last = path.split(".")
    node = d
    for p in head:
        node = node[int(p)] if p.isdigit() else node[p]
    if last.isdigit():
        node[int(last)] = value
    else:
        node[last] = value
    return d
