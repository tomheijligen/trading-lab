"""Explicit schedules used by the daily historical backfill."""
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .historical_data import DailyCalendar, SessionCalendar


class CalendarError(ValueError):
    pass


class CryptoCalendar(DailyCalendar):
    timezone = UTC

    def close_at(self, day):
        return datetime.combine(day + timedelta(days=1), time.min, tzinfo=UTC)


@dataclass(frozen=True)
class VenueCalendar(SessionCalendar):
    exchange: str
    timezone: ZoneInfo
    start: date
    end: date
    closes: dict

    def is_session(self, day):
        if not self.start <= day <= self.end:
            raise CalendarError("Calendar unreliable: requested date outside the supplied schedule.")
        return day in self.closes

    def close_at(self, day):
        return self.closes.get(day)


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
        return CryptoCalendar()
    if filename is None:
        raise CalendarError("Calendar unreliable: supply a complete schedule for the instrument's MIC.")
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
