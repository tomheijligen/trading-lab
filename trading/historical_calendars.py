"""Explicit schedules used by the daily historical backfill."""
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .historical_data import DailyCalendar, SessionCalendar


class CalendarError(ValueError):
    pass


class CryptoCalendar(DailyCalendar):
    timezone = UTC
    source = "daily_utc"

    def open_at(self, day):
        return datetime.combine(day, time.min, tzinfo=UTC)

    def close_at(self, day):
        return datetime.combine(day + timedelta(days=1), time.min, tzinfo=UTC)


@dataclass(frozen=True)
class VenueCalendar(SessionCalendar):
    exchange: str
    timezone: ZoneInfo
    start: date
    end: date
    closes: dict
    source = "manual"

    def is_session(self, day):
        if not self.start <= day <= self.end:
            raise CalendarError("Calendar unreliable: requested date outside the supplied schedule.")
        return day in self.closes

    def close_at(self, day):
        return self.closes.get(day)


EXCHANGE_ZONES = {
    "XNAS": "America/New_York", "XNYS": "America/New_York",
    "XAMS": "Europe/Amsterdam", "XBRU": "Europe/Brussels",
}
FIRST_YEAR = 2000
LAST_YEAR = 2030
CONFIRMED_THROUGH = {
    "XNAS": date(2026, 12, 31),
    "XNYS": date(2028, 12, 31),
    # Euronext has published 2027 holidays. Exact year-end half-day hours follow later.
    "XAMS": date(2027, 12, 23),
    "XBRU": date(2027, 12, 23),
}


@lru_cache(maxsize=32)
def _automatic_sessions(exchange, year):
    try:
        from exchange_calendars import get_calendar

        calendar = get_calendar(exchange, start=date(year, 1, 1), end=date(year, 12, 31))
        return {
            session.date(): (opening.to_pydatetime(), close.to_pydatetime())
            for session, opening, close in calendar.schedule[["open", "close"]].itertuples()
        }
    except (ImportError, ValueError, KeyError) as error:
        raise CalendarError("Calendar unreliable: automatic exchange schedule unavailable.") from error


@dataclass(frozen=True)
class AutomaticCalendar(SessionCalendar):
    exchange: str
    timezone: ZoneInfo
    source = "exchange_calendars"

    def require_confirmed(self, end):
        if end > CONFIRMED_THROUGH[self.exchange]:
            raise CalendarError(
                f"Calendar unreliable: {self.exchange} beyond {CONFIRMED_THROUGH[self.exchange]} "
                "is projected; supply a confirmed venue schedule."
            )

    def is_session(self, day):
        return self._session(day) is not None

    def _session(self, day):
        if not FIRST_YEAR <= day.year <= LAST_YEAR:
            raise CalendarError(
                f"Calendar unreliable: automatic schedules cover {FIRST_YEAR}–{LAST_YEAR}."
            )
        return _automatic_sessions(self.exchange, day.year).get(day)

    def open_at(self, day):
        session = self._session(day)
        return session[0] if session else None

    def close_at(self, day):
        session = self._session(day)
        return session[1] if session else None


def _unique_keys(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate calendar field.")
        result[key] = value
    return result


def load_calendar(asset_class, exchange, filename=None):
    if asset_class == "crypto":
        if filename is not None:
            raise CalendarError("Crypto backfill uses daily UTC sessions.")
        if exchange != "BINANCE":
            raise CalendarError("Calendar unreliable: no automatic schedule for this crypto venue.")
        return CryptoCalendar()
    if filename is None:
        if exchange not in EXCHANGE_ZONES:
            raise CalendarError("Calendar unreliable: supply a complete schedule for the instrument's MIC.")
        return AutomaticCalendar(exchange, ZoneInfo(EXCHANGE_ZONES[exchange]))
    try:
        data = json.loads(Path(filename).read_text(encoding="utf-8"), object_pairs_hook=_unique_keys)
        if data["exchange"] != exchange:
            raise CalendarError("Calendar unreliable: schedule MIC differs from instrument MIC.")
        zone = ZoneInfo(data["timezone"])
        start = date.fromisoformat(data["start"])
        end = date.fromisoformat(data["end"])
        if start > end or not isinstance(data["sessions"], dict):
            raise ValueError("Invalid schedule bounds or sessions.")
        closes = {}
        for key, timestamp in data["sessions"].items():
            day = date.fromisoformat(key)
            close = datetime.fromisoformat(timestamp)
            if (
                day in closes or not start <= day <= end or close.utcoffset() is None
                or close.astimezone(zone).date() != day
            ):
                raise ValueError("Invalid session close.")
            closes[day] = close
    except CalendarError:
        raise
    except (OSError, ValueError, TypeError, KeyError, ZoneInfoNotFoundError, OverflowError):
        raise CalendarError("Calendar unreliable: invalid or unreadable venue schedule.") from None
    return VenueCalendar(exchange, zone, start, end, closes)
