"""Plan and run a bounded, manually requested daily historical backfill."""
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from time import sleep

from django.utils import timezone

from .historical_calendars import CalendarError
from .historical_data import (
    SIMULATION_WINDOWS, WARMUP_SESSIONS, expected_sessions, inspect_coverage, store_bars,
    warmup_sessions,
)
from .historical_providers import (
    ALPACA_PAGE_CAP, BINANCE_BAR_WEIGHT, BINANCE_PAIR_WEIGHT, DAY, FETCH_BARS, PAGE_DAYS,
    verify_binance_pair,
)
from .market_data import DataError, TemporaryDataError
from .models import HistoricalBar

MAX_RETRIES = 1


@dataclass(frozen=True)
class BackfillPlan:
    start: date
    end: date
    sessions: tuple
    windows: tuple
    requested_end: date


def _windows(sessions):
    remaining = iter(sessions)
    first = next(remaining, None)
    if first is None:
        return
    last = first
    for day in remaining:
        if (day - first).days >= PAGE_DAYS:
            yield first, last
            first = day
        last = day
    yield first, last


def make_plan(calendar, *, end=None, start=None, days=None):
    today = timezone.now().astimezone(calendar.timezone).date()
    requested_end = end or today - DAY
    if start is not None and days is not None:
        raise ValueError("Choose either --start or --days.")
    if days is not None and days not in SIMULATION_WINDOWS:
        raise ValueError("Unsupported historical backfill window.")
    if requested_end < date(1902, 1, 1):
        raise ValueError("Choose an end date from 1902 onward.")
    start = start or requested_end - timedelta(days=(days or 30) - 1)
    if not 0 <= (requested_end - start).days < max(SIMULATION_WINDOWS):
        raise ValueError("Choose an ordered range of at most 365 calendar days, inclusive.")
    end = min(requested_end, today - DAY)
    if start > end:
        raise ValueError("The requested period contains no completed historical dates.")
    warmup = warmup_sessions(start, calendar)
    if len(warmup) != WARMUP_SESSIONS:
        raise CalendarError("Calendar unreliable: fewer than 30 preceding sessions.")
    period = expected_sessions(start, end, calendar)
    if not period:
        raise ValueError("The requested period contains no trading sessions.")
    sessions = tuple(warmup + period)
    return BackfillPlan(start, end, sessions, tuple(_windows(sessions)), requested_end)


def estimate_requests(provider, windows):
    pages = len(windows)
    if provider == "binance":
        return pages + 1, pages + 1, pages * BINANCE_BAR_WEIGHT + BINANCE_PAIR_WEIGHT
    if provider == "alpaca":
        return pages, pages * ALPACA_PAGE_CAP, pages
    return pages, pages, pages


def _pending_windows(plan, identity):
    existing = set(HistoricalBar.objects.filter(
        **identity, session_date__range=(plan.sessions[0], plan.end),
        quality=HistoricalBar.Quality.VALID,
    ).values_list("session_date", flat=True))
    missing = [day for day in plan.sessions if day not in existing]
    for lo, hi in plan.windows:
        days = [day for day in missing if lo <= day <= hi]
        if days:
            yield days[0], days[-1]


def execute_backfill(provider, identity, calendar, plan):
    """Commit completed chunks separately. A rerun plans from persisted valid sessions."""
    summary = Counter(inserted=0, duplicates=0, rejected=0, requests=0, retries=0)
    quality = Counter()

    def request(fetch):
        for attempt in range(MAX_RETRIES + 1):
            summary["requests"] += 1
            try:
                return fetch()
            except TemporaryDataError as error:
                if attempt == MAX_RETRIES:
                    raise
                summary["retries"] += 1
                sleep(error.retry_after)

    pending = list(_pending_windows(plan, identity))
    failure = None
    try:
        if pending and provider == "binance":
            verify_binance_pair(identity, request)
        for start, end in pending:
            bars = list(FETCH_BARS[provider](identity, calendar, start, end, request))
            counted = store_bars(**identity, bars=bars, calendar=calendar)
            quality.update(counted.pop("quality"))
            summary.update(counted)
    except DataError as error:
        failure = str(error)

    coverage = inspect_coverage(
        **identity, start_date=plan.start, end_date=plan.end, calendar=calendar,
    )["windows"]["requested"]
    return {
        **summary, "missing": len(coverage["gap_dates"]),
        "quality": dict(quality), "coverage": coverage,
    }, failure
