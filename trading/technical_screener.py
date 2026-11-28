"""Read-only technical screening over exact watchlist series."""
from collections import defaultdict
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .historical_calendars import CalendarError, load_calendar
from .historical_data import expected_sessions
from .historical_providers import instrument_identity
from .models import HistoricalBar
from .scanner import scanner_entries
from .technical_indicators import LABELS, calculate_indicators


SERIES_FIELDS = ("provider", "asset_class", "symbol", "exchange", "currency", "adjustment")
LOOKBACK_DAYS = 400


def technical_watchlist():
    """Keep unconfigured entries visible; select exactly one series per instrument."""
    rows = {}
    for entry in scanner_entries():
        group, symbol, exchange = (entry[key] for key in ("group", "symbol", "exchange"))
        key = (group, symbol, exchange)
        row = rows.setdefault(key, {
            "asset_class": group, "symbol": symbol, "exchange": exchange,
            "series": None, "status": "unconfigured",
        })
        if not any(name in entry for name in ("provider", "currency", "adjustment")):
            continue
        try:
            series = instrument_identity(
                entry["provider"], group, symbol, exchange, entry["currency"], entry["adjustment"],
            )
        except (KeyError, TypeError, AttributeError, ValueError):
            row.update(series=None, status="invalid_configuration")
            continue
        if row["status"] == "invalid_configuration" or (row["series"] and row["series"] != series):
            row.update(series=None, status="invalid_configuration")
        else:
            row.update(series=series, status="configured")
    return [rows[key] for key in sorted(rows)]


def _schedule(group, exchange, now):
    calendar = load_calendar(group, exchange)
    end = now.astimezone(calendar.timezone).date()
    calendar.require_confirmed(end)
    start = end - timedelta(days=LOOKBACK_DAYS)
    return {
        day: calendar.close_at(day)
        for day in expected_sessions(start, end, calendar) if calendar.close_at(day) <= now
    }


def technical_rows(watchlist, *, now=None):
    now = now or timezone.now()
    schedules = {}
    wanted = Q()
    rows = [row.copy() for row in watchlist]
    for row in rows:
        identity = row["series"]
        if identity is None:
            continue
        key = (row["asset_class"], identity["exchange"])
        if key not in schedules:
            try:
                schedules[key] = _schedule(*key, now)
            except CalendarError:
                schedules[key] = None
        if schedules[key]:
            wanted |= Q(**identity)

    grouped = defaultdict(dict)
    if wanted:
        bars = HistoricalBar.objects.filter(
            wanted, quality=HistoricalBar.Quality.VALID, observed_at__lte=now,
            session_date__gte=now.date() - timedelta(days=LOOKBACK_DAYS + 1),
            session_date__lte=now.date() + timedelta(days=1),
        ).values_list(
            *SERIES_FIELDS, "session_date", "high", "low", "close", "volume", named=True,
        )
        for bar in bars.iterator(chunk_size=4000):
            grouped[tuple(bar[:len(SERIES_FIELDS)])][bar.session_date] = bar

    for row in rows:
        identity = row["series"]
        if identity is None:
            row["metrics"] = {}
            continue
        schedule = schedules[(row["asset_class"], identity["exchange"])]
        series_key = tuple(identity[field] for field in SERIES_FIELDS)
        row["metrics"] = calculate_indicators(
            tuple(schedule or ()), grouped.get(series_key, {}),
            provider=identity["provider"], currency=identity["currency"],
            asset_class=row["asset_class"], symbol=row["symbol"], calendar_known=schedule is not None,
        )
        row["status"] = row["metrics"]["price"].status
    return rows


def screen_rows(rows, filters):
    ranges = [(key, filters.get(f"{key}_min"), filters.get(f"{key}_max")) for key in LABELS]
    ranges = [(key, low, high) for key, low, high in ranges if low is not None or high is not None]

    def matches(row):
        for key, low, high in ranges:
            metric = row["metrics"].get(key)
            if metric is None or metric.status != "available":
                return False
            if (low is not None and metric.value < low) or (high is not None and metric.value > high):
                return False
        return not ranges or bool(row["metrics"])

    rows = [row for row in rows if matches(row)]
    order = filters.get("sort") or "symbol"
    if order == "symbol":
        return sorted(rows, key=lambda row: row["symbol"], reverse=filters.get("direction") == "desc")
    present, missing = [], []
    for row in rows:
        metric = row["metrics"].get(order)
        (present if metric is not None and metric.value is not None else missing).append(row)
    present.sort(key=lambda row: row["metrics"][order].value, reverse=filters.get("direction") == "desc")
    present.sort(key=lambda row: row["metrics"][order].unit)
    return present + missing
