"""Manual daily FX backfill through the shared Twelve Data client."""
from datetime import date, timedelta
from time import sleep

from django.utils import timezone

from .historical_data import expected_sessions
from .historical_fx import (
    DAY, FX_CALENDAR, FX_SERIES, FX_ZONE, FXInput, fx_available_at, inspect_fx_coverage, store_fx_rates,
)
from .market_data import DataError, TemporaryDataError, twelve_data
from .models import HistoricalFXRate

SOURCE_SYMBOL = "EUR/USD"


def plan_fx_sessions(start, end):
    if not date(1900, 1, 1) <= start <= end or (end - start).days >= 365:
        raise ValueError("Use an ordered range of at most 365 calendar days from 1900 onward.")
    now = timezone.now()
    if end > now.astimezone(FX_ZONE).date():
        raise ValueError("FX backfill cannot request future dates.")
    sessions = expected_sessions(start, end, FX_CALENDAR)
    if not sessions:
        raise ValueError("The FX range contains no expected weekday sessions.")
    if fx_available_at(sessions[-1]) > now:
        raise ValueError("FX backfill requires completed sessions past the availability cutoff.")
    return sessions


def _fetch_twelve_rates(start, end):
    payload = twelve_data(
        "time_series", cache_ttl=timedelta(minutes=5), symbol=SOURCE_SYMBOL,
        interval="1day", adjust="none", order="asc",
        start_date=start.isoformat(), end_date=(end + DAY).isoformat(),
    )
    if (
        not isinstance(payload, dict) or not isinstance(payload.get("meta"), dict)
        or payload["meta"].get("symbol") != SOURCE_SYMBOL
        or payload["meta"].get("interval", "1day") != "1day"
        or not isinstance(payload.get("values"), list)
    ):
        raise DataError("Twelve Data response does not match the canonical daily FX series.")
    for row in payload["values"]:
        try:
            day = date.fromisoformat(row["datetime"])
            if start <= day <= end:
                yield FXInput(day, row["close"])
        except (KeyError, TypeError, ValueError):
            yield None


def backfill_fx(start, end, sessions):
    existing = set(HistoricalFXRate.objects.filter(
        **FX_SERIES, session_date__range=(start, end), quality=HistoricalFXRate.Quality.VALID,
    ).values_list("session_date", flat=True))
    pending = [day for day in sessions if day not in existing]
    result = dict(inserted=0, duplicates=0, rejected=0, conflicts=0, requests=0, retries=0)
    failure = None
    if pending:
        for attempt in range(2):
            result["requests"] += 1
            try:
                result.update(store_fx_rates(_fetch_twelve_rates(pending[0], pending[-1])))
                break
            except TemporaryDataError as error:
                if attempt:
                    failure = str(error)
                    break
                result["retries"] += 1
                sleep(error.retry_after)
            except DataError as error:
                failure = str(error)
                break
    return {**result, "coverage": inspect_fx_coverage(start, end)}, failure
