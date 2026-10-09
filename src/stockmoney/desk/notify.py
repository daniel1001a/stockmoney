"""Push notifications to the owner's phone via ntfy (https://docs.ntfy.sh).

Uses JSON publishing (POST to the server root) so Chinese titles survive
without header encoding. The topic name acts as the password: keep it long
and random, set it in .env as NTFY_TOPIC. Without a topic, messages are only
logged, never dropped silently.
"""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)

INFO, WARN, CRITICAL = 3, 4, 5  # ntfy priorities


def send(title: str, message: str, priority: int = INFO, tags: list[str] | None = None) -> bool:
    topic = os.environ.get("NTFY_TOPIC", "")
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    log.info("notify[%s] %s: %s", priority, title, message)
    if not topic:
        return False
    try:
        r = requests.post(
            server + "/",
            json={
                "topic": topic, "title": title, "message": message[:4000],
                "priority": priority, "tags": tags or [], "markdown": True,
            },
            timeout=10,
        )
        return r.ok
    except requests.RequestException as e:  # a notification failure must never break a trading run
        log.warning("notify failed: %s", e)
        return False
