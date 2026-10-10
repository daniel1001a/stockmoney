"""NYSE trading calendar, self-contained (no dependency).

Used to decide "last trading day of the month" the same way in live runs
(which cannot see tomorrow's bar) and in backtests. Pandas ``BDay`` only skips
weekends, so it mistakes e.g. Thursday 2024-03-28 (Good Friday is the 29th)
for a mid-month day.

Rules implemented (valid for 1998 onward, which covers the desk's data):
New Year's Day, Martin Luther King Jr. Day, Presidents' Day, Good Friday,
Memorial Day, Juneteenth (from 2022), Independence Day, Labor Day,
Thanksgiving, Christmas. A holiday on a Saturday is observed on the Friday
and one on a Sunday on the Monday, except that NYSE does not move New Year's
Day to the previous Friday (so 2027-12-31 trades). A few one-off closures are
listed in ``SPECIAL_CLOSURES``; extend it when the exchange announces one.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache

# Days the exchange closed outside the rule set (national days of mourning,
# hurricane, 9/11). Month-end relevance is rare but keeps the calendar honest.
SPECIAL_CLOSURES = frozenset(
    {
        date(2001, 9, 11), date(2001, 9, 12), date(2001, 9, 13), date(2001, 9, 14),
        date(2004, 6, 11),   # Reagan
        date(2007, 1, 2),    # Ford
        date(2012, 10, 29), date(2012, 10, 30),  # Hurricane Sandy
        date(2018, 12, 5),   # Bush
        date(2025, 1, 9),    # Carter
    }
)


def _as_date(d) -> date:
    if isinstance(d, datetime):  # includes pandas Timestamp
        return d.date()
    if isinstance(d, date):
        return d
    return d.date()  # numpy datetime64 wrappers etc.


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th (1-based) given weekday (Mon=0) of the month."""
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=64)
def nyse_holidays(year: int) -> frozenset[date]:
    """Full-day NYSE closures falling in ``year`` (weekday dates only)."""
    out: set[date] = set()
    jan1 = date(year, 1, 1)
    if jan1.weekday() != 5:  # Saturday New Year's Day is not observed on Dec 31
        out.add(_observed(jan1))
    out.add(_nth_weekday(year, 1, 0, 3))   # MLK
    out.add(_nth_weekday(year, 2, 0, 3))   # Presidents' Day
    out.add(_easter(year) - timedelta(days=2))  # Good Friday
    out.add(_last_weekday(year, 5, 0))     # Memorial Day
    if year >= 2022:
        out.add(_observed(date(year, 6, 19)))  # Juneteenth
    out.add(_observed(date(year, 7, 4)))
    out.add(_nth_weekday(year, 9, 0, 1))   # Labor Day
    out.add(_nth_weekday(year, 11, 3, 4))  # Thanksgiving
    out.add(_observed(date(year, 12, 25)))
    out |= {d for d in SPECIAL_CLOSURES if d.year == year}
    return frozenset(d for d in out if d.year == year)


def is_trading_day(d) -> bool:
    d = _as_date(d)
    return d.weekday() < 5 and d not in nyse_holidays(d.year)


def next_trading_day(d) -> date:
    """First trading day strictly after ``d``."""
    d = _as_date(d) + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def is_last_trading_day_of_month(d) -> bool:
    """True when the next NYSE session falls in a different month. Depends on
    ``d`` only, so a live run (no future bars) and a backtest agree."""
    d = _as_date(d)
    return next_trading_day(d).month != d.month
