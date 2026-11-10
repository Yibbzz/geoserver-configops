"""When a Dataset is due a check: its `update` schedule against the last check.

`sync` runs from a nightly job, so a named schedule allows an hour's slack:
a daily dataset checked at 02:00:30 yesterday is due again at 02:00 today.
"""

from __future__ import annotations

from datetime import datetime, timedelta

NAMED = {"daily": timedelta(days=1), "weekly": timedelta(days=7), "monthly": timedelta(days=30)}
SLACK = timedelta(hours=1)
CRON_FIELDS = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]  # minute hour day month weekday (0 = Sunday)
MAX_CRON_LOOKBACK = timedelta(days=366)


def cron_values(field: str, low: int, high: int) -> set[int]:
    """The values one cron field allows: *, n, a-b, lists and /steps."""
    values = set()
    for part in field.split(","):
        rng, _, step = part.partition("/")
        if rng == "*":
            start, end = low, high
        elif "-" in rng:
            start, end = (int(x) for x in rng.split("-"))
        else:
            start = end = int(rng)
            if step:
                end = high
        values.update(range(start, end + 1, int(step) if step else 1))
    if high == 6 and 7 in values:  # 7 is Sunday too
        values.add(0)
    return values


def cron_matches(expr: str, t: datetime) -> bool:
    fields = expr.split()
    minute, hour, day, month, weekday = (cron_values(f, lo, hi) for f, (lo, hi) in zip(fields, CRON_FIELDS))
    if t.minute not in minute or t.hour not in hour or t.month not in month:
        return False
    day_ok, weekday_ok = t.day in day, (t.isoweekday() % 7) in weekday
    # as in cron: when both day and weekday are restricted, either may match
    if fields[2] != "*" and fields[4] != "*":
        return day_ok or weekday_ok
    return day_ok and weekday_ok


def cron_fired(expr: str, since: datetime, now: datetime) -> bool:
    """Whether the expression names a minute after `since`, up to `now`."""
    t = max(since, now - MAX_CRON_LOOKBACK).replace(second=0, microsecond=0) + timedelta(minutes=1)
    while t <= now:
        if cron_matches(expr, t):
            return True
        t += timedelta(minutes=1)
    return False


def due(update: str | None, last_check: datetime | None, now: datetime | None = None) -> bool:
    """Whether a dataset with this schedule should be checked now. `manual` never is."""
    update = update or "manual"
    if update == "manual":
        return False
    if last_check is None:
        return True
    now = now or datetime.now()
    if update in NAMED:
        return now - last_check >= NAMED[update] - SLACK
    return cron_fired(update, last_check, now)
