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


def cron_parse(expr: str) -> tuple:
    fields = expr.split()
    sets = tuple(cron_values(f, lo, hi) for f, (lo, hi) in zip(fields, CRON_FIELDS))
    # as in cron: when both day and weekday are restricted, either may match
    return sets + (fields[2] != "*" and fields[4] != "*",)


def cron_matches(expr: str | tuple, t: datetime) -> bool:
    minute, hour, day, month, weekday, either = cron_parse(expr) if isinstance(expr, str) else expr
    if t.minute not in minute or t.hour not in hour or t.month not in month:
        return False
    day_ok, weekday_ok = t.day in day, (t.isoweekday() % 7) in weekday
    return (day_ok or weekday_ok) if either else (day_ok and weekday_ok)


def cron_next(expr: str, after: datetime, limit: timedelta = MAX_CRON_LOOKBACK) -> datetime | None:
    """The first minute after `after` the expression names (None if none within `limit`)."""
    parsed = cron_parse(expr)
    t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    end = after + limit
    while t <= end:
        if cron_matches(parsed, t):
            return t
        t += timedelta(minutes=1)
    return None


def cron_fired(expr: str, since: datetime, now: datetime) -> bool:
    """Whether the expression names a minute after `since`, up to `now`."""
    start = max(since, now - MAX_CRON_LOOKBACK)
    found = cron_next(expr, start, now - start)
    return found is not None and found <= now


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


def next_check(update: str | None, last_check: datetime | None, now: datetime | None = None) -> datetime | None:
    """
    When the dataset is next due: now, if it already is; None for `manual`.
    (`sync` runs from a scheduled job, so "due" means "at the next run".)
    """
    update = update or "manual"
    if update == "manual":
        return None
    now = now or datetime.now()
    if due(update, last_check, now):
        return now
    if update in NAMED:
        return last_check + NAMED[update] - SLACK
    return cron_next(update, now)
