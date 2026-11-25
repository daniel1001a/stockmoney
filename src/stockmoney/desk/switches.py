"""Owner kill switches (2026-11-25), read from the environment (.env, loaded by
scripts/desk_job.sh). Both default to OFF: unset, empty or any value other than
1 / true / yes / on means off.

- ``GROK_MAIL_ENABLED``: the desk mails nothing to Grok (GROK_ADDRESS). That
  covers DESK_BRIEF, PLAN_REMINDER, RESEARCH_REQUEST to Grok, FEEDBACK on the
  Grok channel and PING to Grok. Mail to MUSE_ADDRESS (the owner's own Gmail:
  WATCHLIST, REPORT, Muse's RESEARCH_REQUEST) is not affected. Grok's inbound
  mail is still read and stored.
- ``OPTIONS_ENABLED``: the intraday options sleeve does not run. ``options``
  (session) and ``options-recover`` exit at once without touching the broker,
  so no option order of any kind is placed and nothing waits for Grok's
  OPTIONS_PLAN; the defined-risk spread shadow (options.spreads tick) is
  skipped too. The stock jobs never depended on either.

Turning one back on is one line in .env, e.g. ``GROK_MAIL_ENABLED=1``.
"""
from __future__ import annotations

import os

GROK_MAIL = "GROK_MAIL_ENABLED"
OPTIONS = "OPTIONS_ENABLED"
_ON = {"1", "true", "yes", "on"}


def _flag(name: str, env=None) -> bool:
    env = os.environ if env is None else env
    return str(env.get(name) or "").strip().lower() in _ON


def grok_mail_enabled(env=None) -> bool:
    return _flag(GROK_MAIL, env)


def options_enabled(env=None) -> bool:
    return _flag(OPTIONS, env)
