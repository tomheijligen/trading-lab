"""Historical bar ingestion, persistence, and coverage validation."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import groupby
from sqlite3 import SQLITE_CONSTRAINT_UNIQUE
from statistics import median

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import HistoricalBar, ZERO

SIMULATION_WINDOWS = (30, 90, 180, 365)
WARMUP_SESSIONS = 30
SPIKE_THRESHOLD = Decimal("0.50")
PRICE_LIMIT = Decimal("1000000000")
VOLUME_LIMIT = Decimal("9999999999999999999999.99999999")
BAR_KEY = ("provider", "asset_class", "symbol", "exchange", "currency", "session_date", "adjustment")
BAR_CONSTRAINT = "unique_historical_market_bar"


@dataclass(frozen=True)
class BarInput:
    session_date: date
    observed_at: datetime
    open: object
    high: object
    low: object
    close: object
    volume: object = None


class SessionCalendar:
    """Explicit venue-calendar interface. Holidays must be supplied by caller."""

    def is_session(self, day):
        raise NotImplementedError

    def require_confirmed(self, end):
        """Allow rule-based calendars to reject unconfirmed historical dates."""


@dataclass(frozen=True)
class WeekdayCalendar(SessionCalendar):
    holidays: frozenset = frozenset()

    def is_session(self, day):
        return day.weekday() < 5 and day not in self.holidays


@dataclass(frozen=True)
class DailyCalendar(SessionCalendar):
    """Continuous-market calendar for crypto venues."""

    def is_session(self, day):
        return True


def _number(value, *, optional=False, maximum=PRICE_LIMIT):
    if value is None and optional:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not result.is_finite() or result < ZERO or result > maximum:
        return None
    if not optional and result <= ZERO:
        return None
    return result


def _validated_input(bar):
    try:
        day = date.fromisoformat(bar.session_date) if isinstance(bar.session_date, str) else bar.session_date
        if isinstance(day, datetime) or not isinstance(day, date):
            return None
        timestamp = bar.observed_at
        if not isinstance(timestamp, datetime) or timezone.is_naive(timestamp):
            return None
        raw_volume = bar.volume
        values = {
            "open": _number(bar.open), "high": _number(bar.high),
            "low": _number(bar.low), "close": _number(bar.close),
            "volume": _number(raw_volume, optional=True, maximum=VOLUME_LIMIT),
        }
        valid = not any(values[field] is None for field in ("open", "high", "low", "close"))
        valid = valid and (raw_volume is None or values["volume"] is not None)
        if valid:
            valid = (
                values["high"] >= max(values["open"], values["close"])
                and values["low"] <= min(values["open"], values["close"])
                and values["high"] >= values["low"]
            )
        return {
            "session_date": day, "observed_at": timestamp, **values,
            "quality": HistoricalBar.Quality.VALID if valid else HistoricalBar.Quality.INVALID,
            "quality_detail": "" if valid else "Invalid or inconsistent OHLCV values.",
        }
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def validate_bars(bars, *, calendar=None):
    """Classify each input. Missing dates are assessed separately from row quality."""
    prepared = []
    for bar in bars:
        prepared.append(_validated_input(bar) or {
            "quality": HistoricalBar.Quality.INVALID,
            "quality_detail": "Missing or invalid session date or observation timestamp.",
        })
    prepared.sort(key=lambda entry: (
        entry.get("session_date", date.max),
        entry.get("observed_at") is None,
        entry.get("observed_at"),
    ))

    accepted = []
    for day, entries in groupby(prepared, key=lambda entry: entry.get("session_date")):
        candidates = [entry for entry in entries if entry["quality"] == HistoricalBar.Quality.VALID]
        if not candidates:
            continue
        if calendar is not None:
            calendar.require_confirmed(day)
        first, *repeats = candidates
        prices = ("open", "high", "low", "close", "volume")
        reference = median(accepted[-20:]) if accepted else None
        if calendar is not None and not calendar.is_session(day):
            quality = HistoricalBar.Quality.INVALID
            detail = "Date is outside the supplied venue calendar."
        elif any(any(row[field] != first[field] for field in prices) for row in repeats):
            quality = HistoricalBar.Quality.INVALID
            detail = "Conflicting OHLCV values for the same source session."
        elif reference and abs(first["close"] / reference - 1) > SPIKE_THRESHOLD:
            quality = HistoricalBar.Quality.SPIKE
            detail = "Close moved more than 50% from the recent median."
        else:
            accepted.append(first["close"])
            candidates = repeats
            quality = HistoricalBar.Quality.DUPLICATE
            detail = "Repeated OHLCV values for the same source session."
        for row in candidates:
            row.update(quality=quality, quality_detail=detail)
    return prepared


def expected_sessions(start_date, end_date, calendar):
    """Enumerate only sessions declared by the supplied exchange calendar."""
    sessions = []
    day = start_date
    while day <= end_date:
        if calendar.is_session(day):
            sessions.append(day)
        day += timedelta(days=1)
    return sessions


def _check_metadata(provider, asset_class, symbol, exchange, currency, adjustment):
    if asset_class not in {"stock", "etf", "crypto"}:
        raise ValueError("Historical bars support stocks, ETFs, and crypto.")
    if not all(isinstance(value, str) and value.strip() for value in
               (provider, symbol, exchange, currency, adjustment)):
        raise ValueError("Provider, instrument, venue, currency, and adjustment are required.")
    if adjustment not in HistoricalBar.Adjustment.values:
        raise ValueError("Unsupported adjustment status.")


def _is_bar_key_conflict(error):
    cause = error.__cause__
    if getattr(cause, "sqlstate", None) == "23505":
        return cause.diag.constraint_name == BAR_CONSTRAINT
    if getattr(cause, "sqlite_errorcode", None) == SQLITE_CONSTRAINT_UNIQUE:
        table = HistoricalBar._meta.db_table
        columns = ", ".join(f"{table}.{field}" for field in BAR_KEY)
        return str(cause) == f"UNIQUE constraint failed: {columns}"
    return False


def store_bars(*, provider, asset_class, symbol, exchange, currency, adjustment,
               bars, calendar=None):
    """Store valid source bars, preserving the first accepted row for each key."""
    _check_metadata(provider, asset_class, symbol, exchange, currency, adjustment)
    identity = {
        "provider": provider, "asset_class": asset_class, "symbol": symbol,
        "exchange": exchange, "currency": currency, "adjustment": adjustment,
    }
    result = {"inserted": 0, "duplicates": 0, "rejected": 0, "quality": {}}
    prepared = validate_bars(bars, calendar=calendar)
    with transaction.atomic():
        for entry in prepared:
            quality = entry["quality"]
            if quality == HistoricalBar.Quality.VALID:
                try:
                    with transaction.atomic():
                        HistoricalBar.objects.create(**identity, **entry)
                except IntegrityError as error:
                    if not _is_bar_key_conflict(error):
                        raise
                    quality = HistoricalBar.Quality.DUPLICATE

            if quality == HistoricalBar.Quality.VALID:
                result["inserted"] += 1
            elif quality == HistoricalBar.Quality.DUPLICATE:
                result["duplicates"] += 1
            else:
                result["rejected"] += 1
            result["quality"][quality] = result["quality"].get(quality, 0) + 1
    return result


def warmup_sessions(start, calendar):
    # Limit the search in case the calendar has no session data for this period.
    days = []
    for offset in range(1, min(366, (start - date.min).days) + 1):
        day = start - timedelta(days=offset)
        if calendar.is_session(day):
            days.append(day)
            if len(days) == WARMUP_SESSIONS:
                break
    return sorted(days)


def inspect_coverage(*, provider, asset_class, symbol, exchange, currency, adjustment,
                     end_date=None, calendar=None, start_date=None):
    """Report coverage for one exact series using the caller's venue calendar."""
    _check_metadata(provider, asset_class, symbol, exchange, currency, adjustment)
    end_date = end_date or timezone.localdate()
    if calendar is not None:
        calendar.require_confirmed(end_date)
    identity = {
        "provider": provider, "asset_class": asset_class, "symbol": symbol,
        "exchange": exchange, "currency": currency, "adjustment": adjustment,
    }
    series = HistoricalBar.objects.filter(
        **identity, session_date__lte=end_date,
    )
    if start_date is not None:
        if start_date > end_date:
            raise ValueError("Coverage start date must be on or before its end date.")
        preceding = warmup_sessions(start_date, calendar) if calendar is not None else []
        series = series.filter(session_date__gte=preceding[0] if preceding else start_date)
    qualities_by_day = dict(series.values_list("session_date", "quality"))
    valid_days = {
        day for day, quality in qualities_by_day.items()
        if quality == HistoricalBar.Quality.VALID
        and (calendar is None or calendar.is_session(day))
    }
    report = {
        **identity, "end_date": end_date.isoformat(), "available": len(valid_days),
        "windows": {},
        "quality_counts": dict.fromkeys(HistoricalBar.Quality.values, 0),
    }
    for quality in qualities_by_day.values():
        report["quality_counts"][quality] += 1

    windows = (
        {"requested": start_date} if start_date is not None else
        {days: end_date - timedelta(days=days) for days in SIMULATION_WINDOWS}
    )
    for key, start in windows.items():
        period = expected_sessions(start, end_date, calendar) if calendar is not None else []
        warmup = warmup_sessions(start, calendar) if calendar is not None else []
        missing = [day.isoformat() for day in period if day not in qualities_by_day]
        rejected = [
            day.isoformat() for day in period
            if day in qualities_by_day and day not in valid_days
        ]
        warmup_unusable = sorted(day.isoformat() for day in warmup if day not in valid_days)
        warmup_ready = len(warmup) == WARMUP_SESSIONS and not warmup_unusable
        available = sum(start <= day <= end_date for day in valid_days)
        report["windows"][key] = {
            "start_date": start.isoformat(),
            "available_bars": available,
            "expected_bars": len(period) if calendar is not None else None,
            "warmup_bars": len(warmup) - len(warmup_unusable),
            "warmup_ready": warmup_ready,
            "warmup_rejected_bars": sum(
                day in qualities_by_day and day not in valid_days for day in warmup
            ),
            "warmup_unusable_dates": warmup_unusable,
            "has_period_data": bool(available),
            "covered": bool(period) and warmup_ready and not missing and not rejected,
            "calendar_status": "checked" if calendar is not None else "unavailable",
            "gap_dates": missing,
            "rejected_dates": rejected,
        }
    return report
