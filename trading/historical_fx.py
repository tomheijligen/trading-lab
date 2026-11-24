"""Daily EUR/USD storage and point-in-time FX lookup."""
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .historical_data import WeekdayCalendar, expected_sessions
from .market_data import TWELVE
from .models import HistoricalFXRate

DAY = timedelta(days=1)
FX_ZONE = ZoneInfo("Australia/Sydney")
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


@dataclass(frozen=True)
class FXPolicy:
    calendar: WeekdayCalendar
    delay: timedelta
    max_age: timedelta

    def available_at(self, day):
        return datetime.combine(day + DAY, time.min, FX_ZONE) + self.delay


def get_fx_policy():
    try:
        delay = int(settings.HISTORICAL_FX_PUBLICATION_DELAY_MINUTES)
        age = int(settings.HISTORICAL_FX_MAX_AGE_HOURS)
        holidays = frozenset(
            date.fromisoformat(value.strip())
            for value in settings.HISTORICAL_FX_HOLIDAYS.split(",") if value.strip()
        )
        if not 0 <= delay <= 120 or not 1 <= age <= 96:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("Invalid historical FX policy: check the delay, age and ISO holiday dates.") from None
    return FXPolicy(WeekdayCalendar(holidays), timedelta(minutes=delay), timedelta(hours=age))


def fx_available_at(day):
    """Return the modeled availability cutoff, not a provider publication timestamp."""
    return get_fx_policy().available_at(day)


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
    policy = get_fx_policy()
    today = now.astimezone(FX_ZONE).date()
    for entry in rates:
        if (
            not isinstance(entry, FXInput) or type(entry.session_date) is not date
            or not policy.calendar.is_session(entry.session_date)
            or entry.session_date >= today
            or policy.available_at(entry.session_date) > now
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
                defaults={"rate": rate, "quality": quality, "available_at": policy.available_at(day)},
            )
            if created:
                result["inserted"] += 1
                result["duplicates"] += len(records) - 1
            elif (existing.rate, existing.quality) == (rate, quality):
                result["duplicates"] += len(records)
            else:
                result["conflicts"] += len(records)
    return dict(result)


def inspect_fx_coverage(start, end):
    if start > end:
        raise ValueError("FX coverage start must be on or before the end.")
    policy, now = get_fx_policy(), timezone.now()
    sessions = [
        day for day in expected_sessions(start, end, policy.calendar)
        if policy.available_at(day) <= now
    ]
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


def _valuation_session(as_of, policy):
    local = as_of.astimezone(FX_ZONE)
    day = local.date() - DAY
    while not policy.calendar.is_session(day) or policy.available_at(day) > as_of:
        day -= DAY
    return day


def _quote_issue(quote, session, as_of, policy, max_age):
    modeled_at = policy.available_at(session).astimezone(UTC)
    if as_of - modeled_at > max_age:
        return "stale"
    if quote is None:
        return "missing"
    if quote.quality != HistoricalFXRate.Quality.VALID:
        return "suspect"
    stored_at = quote.available_at.astimezone(UTC)
    if stored_at > as_of:
        return "unavailable_at_valuation"
    if as_of - stored_at > max_age:
        return "stale"
    return None


def inspect_fx_valuations(valuations):
    """Check the same historical FX rules used by conversion for each venue close."""
    policy = get_fx_policy()
    sessions = {
        day: _valuation_session(close.astimezone(UTC), policy)
        for day, close in valuations.items()
    }
    quotes = {
        row.session_date: row for row in HistoricalFXRate.objects.filter(
            **FX_SERIES, session_date__in=set(sessions.values()),
        )
    }
    gaps = []
    for day, close in valuations.items():
        session = sessions[day]
        issue = _quote_issue(quotes.get(session), session, close.astimezone(UTC), policy, policy.max_age)
        if issue:
            gaps.append({"session_date": day.isoformat(), "fx_date": session.isoformat(), "reason": issue})
    return {
        "series": FX_SERIES, "availability": "modeled", "timezone": FX_ZONE.key,
        "publication_delay_minutes": policy.delay // timedelta(minutes=1),
        "max_age_hours": policy.max_age // timedelta(hours=1),
        "expected_valuations": len(valuations), "covered_valuations": len(valuations) - len(gaps),
        "gaps": gaps, "covered": bool(valuations) and not gaps,
    }


def historical_fx_rate(source, target, *, as_of, max_age=None):
    """Return target units per source unit as of an aware valuation timestamp."""
    if not isinstance(as_of, datetime) or timezone.is_naive(as_of):
        raise ValueError("FX valuation requires an offset-aware timestamp.")
    policy = get_fx_policy()
    max_age = policy.max_age if max_age is None else max_age
    if not isinstance(max_age, timedelta) or not timedelta(0) < max_age <= policy.max_age:
        raise ValueError("FX maximum age must be positive and within the configured maximum.")
    if {source, target} - {"EUR", "USD"}:
        raise ValueError("Historical FX currently supports EUR and USD.")
    if source == target:
        return Decimal(1)

    as_of = as_of.astimezone(UTC)
    session = _valuation_session(as_of, policy)
    quote = HistoricalFXRate.objects.filter(
        **FX_SERIES, session_date=session,
    ).first()
    issue = _quote_issue(quote, session, as_of, policy, max_age)
    if issue == "stale":
        raise HistoricalFXUnavailable(f"FX rate for {session.isoformat()} exceeds the maximum age.")
    if issue:
        raise HistoricalFXUnavailable(f"No usable FX rate for expected session {session.isoformat()} ({issue}).")
    with localcontext() as context:
        context.prec = 38
        return quote.rate if source == "EUR" else Decimal(1) / quote.rate


def convert_historical_fx(amount, source, target, *, as_of, max_age=None):
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
