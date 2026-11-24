"""Daily EUR/USD storage and point-in-time FX lookup."""
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from .historical_data import WeekdayCalendar, expected_sessions
from .market_data import TWELVE
from .models import HistoricalFXRate

DAY = timedelta(days=1)
FX_ZONE = ZoneInfo("Australia/Sydney")
FX_CALENDAR = WeekdayCalendar()
FX_MAX_AGE = timedelta(hours=96)
FX_PRECISION = Decimal("0.000000000001")
FX_LIMIT = Decimal("999999999999.999999999999")
FX_SERIES = {"provider": TWELVE.name, "base_currency": "EUR", "quote_currency": "USD"}


class HistoricalFXUnavailable(LookupError):
    pass


@dataclass(frozen=True)
class FXInput:
    session_date: date
    rate: object
    quality: str = HistoricalFXRate.Quality.VALID


def fx_available_at(day):
    """Allow daily closes from 01:00 Sydney time on the following calendar day."""
    return datetime.combine(day + DAY, time(1), FX_ZONE)


def _normalize_rate(raw):
    try:
        rate = Decimal(str(raw))
        if rate.is_finite() and FX_PRECISION <= rate <= FX_LIMIT:
            return rate.quantize(FX_PRECISION)
    except (InvalidOperation, TypeError, ValueError):
        pass
    return None


def store_fx_rates(rates):
    """Store one canonical quote per source session and surface conflicting revisions."""
    result = Counter(inserted=0, duplicates=0, rejected=0, conflicts=0)
    sessions = defaultdict(list)
    now = timezone.now()
    today = now.astimezone(FX_ZONE).date()
    for entry in rates:
        if (
            not isinstance(entry, FXInput) or type(entry.session_date) is not date
            or not FX_CALENDAR.is_session(entry.session_date)
            or entry.session_date >= today
            or fx_available_at(entry.session_date) > now
            or entry.quality not in HistoricalFXRate.Quality.values
        ):
            result["rejected"] += 1
            continue
        rate = _normalize_rate(entry.rate)
        if rate is None:
            result["rejected"] += 1
        else:
            sessions[entry.session_date].append((rate, entry.quality))

    with transaction.atomic():
        for day, records in sorted(sessions.items()):
            if len(set(records)) != 1:
                result["conflicts"] += len(records)
                continue
            rate, quality = records[0]
            existing, created = HistoricalFXRate.objects.get_or_create(
                **FX_SERIES, session_date=day,
                defaults={"rate": rate, "quality": quality, "available_at": fx_available_at(day)},
            )
            if created:
                result["inserted"] += 1
                result["duplicates"] += len(records) - 1
            elif (existing.rate, existing.quality, existing.available_at) == (
                rate, quality, fx_available_at(day),
            ):
                result["duplicates"] += len(records)
            else:
                result["conflicts"] += len(records)
    return dict(result)


def inspect_fx_coverage(start, end):
    if start > end:
        raise ValueError("FX coverage start must be on or before the end.")
    sessions = expected_sessions(start, end, FX_CALENDAR)
    stored = dict(HistoricalFXRate.objects.filter(
        **FX_SERIES, session_date__range=(start, end),
    ).values_list("session_date", "quality"))
    missing = [day.isoformat() for day in sessions if day not in stored]
    suspect = [
        day.isoformat() for day in sessions
        if day in stored and stored[day] != HistoricalFXRate.Quality.VALID
    ]
    return {
        "expected_sessions": len(sessions), "valid_rates": len(sessions) - len(missing) - len(suspect),
        "missing_dates": missing, "suspect_dates": suspect,
        "covered": bool(sessions) and not missing and not suspect,
    }


def _valuation_session(as_of):
    local = as_of.astimezone(FX_ZONE)
    day = local.date() - DAY
    if fx_available_at(day) > as_of:
        day -= DAY
    while not FX_CALENDAR.is_session(day):
        day -= DAY
    return day


def historical_fx_rate(source, target, *, as_of, max_age=FX_MAX_AGE):
    """Return target units per source unit as of an aware valuation timestamp."""
    if not isinstance(as_of, datetime) or timezone.is_naive(as_of):
        raise ValueError("FX valuation requires an offset-aware timestamp.")
    if not isinstance(max_age, timedelta) or not timedelta(0) < max_age <= FX_MAX_AGE:
        raise ValueError("FX maximum age must be positive and at most 96 hours.")
    if {source, target} - {"EUR", "USD"}:
        raise ValueError("Historical FX currently supports EUR and USD.")
    if source == target:
        return Decimal(1)

    as_of = as_of.astimezone(UTC)
    session = _valuation_session(as_of)
    if as_of - fx_available_at(session).astimezone(UTC) > max_age:
        raise HistoricalFXUnavailable(f"FX rate for {session.isoformat()} exceeds the maximum age.")
    quote = HistoricalFXRate.objects.filter(
        **FX_SERIES, session_date=session, quality=HistoricalFXRate.Quality.VALID,
        available_at__lte=as_of, available_at__gte=as_of - max_age,
    ).first()
    if quote is None:
        raise HistoricalFXUnavailable(f"No usable FX rate for expected session {session.isoformat()}.")
    with localcontext() as context:
        context.prec = 38
        return quote.rate if source == "EUR" else Decimal(1) / quote.rate


def convert_historical_fx(amount, source, target, *, as_of, max_age=FX_MAX_AGE):
    """Convert a finite amount without rounding it to account precision."""
    try:
        amount = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("FX conversion requires a finite amount.") from None
    if not amount.is_finite():
        raise ValueError("FX conversion requires a finite amount.")
    rate = historical_fx_rate(source, target, as_of=as_of, max_age=max_age)
    with localcontext() as context:
        context.prec = 38
        return amount * rate
