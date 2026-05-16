"""Temporal expression parser for memory queries.

Extracts date ranges from natural language temporal expressions like
"last month", "recently", "3 years ago", "since January".
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone


def _subtract_months(dt: datetime, months: int) -> datetime:
    """Subtract months from a datetime, handling year rollover."""
    month = dt.month - months
    year = dt.year
    while month <= 0:
        month += 12
        year -= 1
    day = min(dt.day, _days_in_month(year, month))
    return dt.replace(year=year, month=month, day=day)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        next_month = datetime(year + 1, 1, 1)
    else:
        next_month = datetime(year, month + 1, 1)
    return (next_month - datetime(year, month, 1)).days


_TEMPORAL_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(\d+)\s+(days?|weeks?|months?|years?)\s+ago", re.I), "ago"),
    (re.compile(r"last\s+(\d+)\s+(days?|weeks?|months?|years?)", re.I), "last_n"),
    (re.compile(r"\b(recently|lately)\b", re.I), "recently"),
    (re.compile(r"\blast\s+(month|year|week)\b", re.I), "last_period"),
    (re.compile(r"\bthis\s+(month|year|week)\b", re.I), "this_period"),
    (re.compile(r"\bsince\s+(january|february|march|april|may|june|july|august|september|october|november|december)\b", re.I), "since_month"),
    (re.compile(r"\bbefore\s+(january|february|march|april|may|june|july|august|september|october|november|december)\b", re.I), "before_month"),
]

_UNIT_DAYS = {"day": 1, "days": 1, "week": 7, "weeks": 7, "month": 30, "months": 30, "year": 365, "years": 365}

_MONTH_NUMBERS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def parse_temporal(query: str) -> dict | None:
    """Extract temporal constraints from a query string.

    Returns {"after": datetime | None, "before": datetime | None, "boost": float} or None.
    """
    for pattern, op in _TEMPORAL_PATTERNS:
        match = pattern.search(query)
        if not match:
            continue
        now = datetime.now(timezone.utc)
        result = _resolve(op, match, now)
        if result:
            return result
    return None


def _resolve(op: str, match: re.Match, now: datetime) -> dict | None:
    if op == "ago":
        n = int(match.group(1))
        unit = match.group(2).lower()
        after = _offset(now, n, unit)
        return {"after": after, "before": None, "boost": 1.5}

    if op == "last_n":
        n = int(match.group(1))
        unit = match.group(2).lower()
        after = _offset(now, n, unit)
        return {"after": after, "before": None, "boost": 1.3}

    if op == "recently":
        return {"after": now - timedelta(days=14), "before": None, "boost": 1.2}

    if op == "last_period":
        period = match.group(1).lower()
        if period == "week":
            after = now - timedelta(weeks=1)
        elif period == "month":
            after = _subtract_months(now, 1)
        else:
            after = now.replace(year=now.year - 1)
        return {"after": after, "before": None, "boost": 1.3}

    if op == "this_period":
        period = match.group(1).lower()
        if period == "week":
            after = now - timedelta(days=now.weekday())
        elif period == "month":
            after = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            after = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return {"after": after, "before": None, "boost": 1.2}

    if op == "since_month":
        month_name = match.group(1).lower()
        month_num = _MONTH_NUMBERS[month_name]
        year = now.year if month_num <= now.month else now.year - 1
        after = now.replace(year=year, month=month_num, day=1, hour=0, minute=0, second=0, microsecond=0)
        return {"after": after, "before": None, "boost": 1.2}

    if op == "before_month":
        month_name = match.group(1).lower()
        month_num = _MONTH_NUMBERS[month_name]
        year = now.year if month_num > now.month else now.year - 1
        before = now.replace(year=year, month=month_num, day=1, hour=0, minute=0, second=0, microsecond=0)
        return {"after": None, "before": before, "boost": 1.3}

    return None


def _offset(now: datetime, n: int, unit: str) -> datetime:
    if unit in ("month", "months"):
        return _subtract_months(now, n)
    if unit in ("year", "years"):
        return now.replace(year=now.year - n)
    return now - timedelta(days=n * _UNIT_DAYS[unit])
