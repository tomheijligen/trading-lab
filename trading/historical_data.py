"""Historical bar ingestion, persistence, and coverage validation."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from statistics import median

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import HistoricalBar, ZERO

SIMULATION_WINDOWS = (30, 90, 180, 365)
WARMUP_SESSIONS = 30
SPIKE_THRESHOLD = Decimal("0.50")
PRICE_LIMIT = Decimal("1000000000")
VOLUME_LIMIT = Decimal("1e22")


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


def validate_bars(bars, *, expected_sessions=None, end_date=None):
    """Return every source row, flagging duplicates, gaps and suspicious prices."""
    prepared = []
    for bar in bars:
        entry = _validated_input(bar)
        if entry:
            prepared.append(entry)
    prepared.sort(key=lambda entry: (entry["session_date"], entry["observed_at"]))

    counts = {}
    for entry in prepared:
        day = entry["session_date"]
        counts[day] = counts.get(day, 0) + 1
    duplicate_days = {day for day, count in counts.items() if count > 1}
    for entry in prepared:
        if entry["session_date"] in duplicate_days:
            entry["quality"] = HistoricalBar.Quality.DUPLICATE
            entry["quality_detail"] = "Multiple input records share this source session date."

    duplicate_days = {
        entry["session_date"] for entry in prepared
        if entry["quality"] == HistoricalBar.Quality.DUPLICATE
    }
    accepted = []
    for entry in prepared:
        if (
            entry["quality"] != HistoricalBar.Quality.VALID
            or entry["session_date"] in duplicate_days
        ):
            continue
        reference = median([close for _, close in accepted[-20:]]) if accepted else None
        if reference and abs(entry["close"] / reference - 1) > SPIKE_THRESHOLD:
            entry["quality"] = HistoricalBar.Quality.SPIKE
            entry["quality_detail"] = (
                "Close moved more than 50% from the recent median. "
                "Review the source and corporate actions."
            )
        else:
            accepted.append((entry["session_date"], entry["close"]))

    if expected_sessions is not None and end_date is not None:
        present = {entry["session_date"] for entry in prepared}
        missing = sorted(set(expected_sessions) - present)
        if missing:
            detail = (
                f"{len(missing)} expected market sessions are missing "
                f"through {end_date.isoformat()}."
            )
            for entry in prepared:
                if entry["quality"] == HistoricalBar.Quality.VALID:
                    entry["quality"] = HistoricalBar.Quality.GAP
                    entry["quality_detail"] = detail
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


def store_bars(*, provider, asset_class, symbol, exchange, currency, adjustment,
               bars, calendar=None):
    """Insert source-currency bars idempotently, preserving the first provider record."""
    _check_metadata(provider, asset_class, symbol, exchange, currency, adjustment)
    prepared = validate_bars(bars)
    if calendar and prepared:
        expected = expected_sessions(
            prepared[0]["session_date"], prepared[-1]["session_date"], calendar,
        )
        prepared = validate_bars(
            bars, expected_sessions=expected, end_date=prepared[-1]["session_date"],
        )

    records = [
        HistoricalBar(
            provider=provider, asset_class=asset_class, symbol=symbol,
            exchange=exchange, currency=currency, adjustment=adjustment, **values,
        )
        for values in prepared
    ]
    if not records:
        return {"inserted": 0, "duplicates": 0, "quality": {}}

    inserted = 0
    with transaction.atomic():
        for record in records:
            if record.quality == HistoricalBar.Quality.INVALID:
                continue
            try:
                with transaction.atomic():
                    record.save(force_insert=True)
                    inserted += 1
            except IntegrityError:
                # A database uniqueness conflict is an idempotent replay.
                # Existing source values and their ingestion timestamps are preserved.
                continue

    quality = {}
    for record in records:
        quality[record.quality] = quality.get(record.quality, 0) + 1
    return {
        "inserted": inserted,
        "duplicates": len(records) - inserted,
        "quality": quality,
    }


def inspect_coverage(*, provider, asset_class, symbol, exchange, currency,
                     end_date=None, calendar=None):
    """Report usable bars and 30/90/180/365-day warm-up coverage."""
    end_date = end_date or timezone.localdate()
    rows = list(HistoricalBar.objects.filter(
        provider=provider, asset_class=asset_class, symbol=symbol,
        exchange=exchange, currency=currency,
        session_date__lte=end_date, quality=HistoricalBar.Quality.VALID,
    ).order_by("session_date"))
    quality_rows = list(HistoricalBar.objects.filter(
        provider=provider, asset_class=asset_class, symbol=symbol,
        exchange=exchange, currency=currency, session_date__lte=end_date,
    ).order_by("session_date"))
    report = {
        "provider": provider, "asset_class": asset_class, "symbol": symbol,
        "exchange": exchange, "currency": currency, "end_date": end_date.isoformat(),
        "available": len(rows), "windows": {},
    }
    for days in SIMULATION_WINDOWS:
        start = end_date - timedelta(days=days)
        window = [row for row in rows if start <= row.session_date <= end_date]
        warmup = [row for row in rows if row.session_date < start]
        all_window = [row for row in quality_rows if start <= row.session_date <= end_date]
        all_warmup = [row for row in quality_rows if row.session_date < start]
        gaps = []
        calendar_status = "unavailable"
        if calendar:
            expected = expected_sessions(start, end_date, calendar)
            present = {row.session_date for row in all_window}
            gaps = [day.isoformat() for day in expected if day not in present]
            calendar_status = "checked"
        report["windows"][days] = {
            "start_date": start.isoformat(),
            "available_bars": len(window),
            "warmup_bars": len(warmup),
            "warmup_ready": len(warmup) >= WARMUP_SESSIONS,
            "warmup_rejected_bars": len(all_warmup) - len(warmup),
            "has_period_data": bool(window),
            "covered": len(warmup) >= WARMUP_SESSIONS and bool(window) and not gaps,
            "calendar_status": calendar_status,
            "gap_dates": gaps,
        }
    report["quality_counts"] = {
        quality: HistoricalBar.objects.filter(
            provider=provider, asset_class=asset_class, symbol=symbol,
            exchange=exchange, currency=currency, quality=quality,
            session_date__lte=end_date,
        ).count()
        for quality, _ in HistoricalBar.Quality.choices
    }
    return report
