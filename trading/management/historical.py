"""Shared instrument arguments for historical data commands."""
from datetime import date

from django.core.management.base import CommandError

from trading.historical_backfill import make_plan
from trading.historical_calendars import CalendarError, load_calendar
from trading.historical_data import SIMULATION_WINDOWS
from trading.historical_providers import PROVIDERS, TWELVE_ADJUSTMENTS, instrument_identity


def add_series_arguments(parser):
    parser.add_argument("--provider", required=True, choices=PROVIDERS)
    parser.add_argument("--asset-class", required=True, choices=("stock", "etf", "crypto"))
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--exchange", required=True, help="Listing MIC for equities or venue for crypto.")
    parser.add_argument("--currency", required=True, help="Source or quote currency.")
    parser.add_argument("--adjustment", choices=TWELVE_ADJUSTMENTS, default="raw")
    parser.add_argument("--calendar-file", help="Optional venue schedule covering the period and warm-up.")
    period = parser.add_mutually_exclusive_group()
    period.add_argument("--start", type=date.fromisoformat)
    period.add_argument("--days", type=int, choices=SIMULATION_WINDOWS)
    parser.add_argument("--end", type=date.fromisoformat, help="Inclusive end; defaults to yesterday at the venue.")


def resolve_series(options):
    try:
        provider = options["provider"]
        identity = instrument_identity(provider, **{
            field: options[field] for field in ("asset_class", "symbol", "exchange", "currency", "adjustment")
        })
        calendar = load_calendar(identity["asset_class"], identity["exchange"], options["calendar_file"])
        if provider == "alpaca" and calendar.timezone.key != "America/New_York":
            raise CalendarError("Calendar unreliable: Alpaca US equity sessions require America/New_York.")
        plan = make_plan(calendar, end=options["end"], start=options["start"], days=options["days"])
        return identity, calendar, plan
    except ValueError as error:
        raise CommandError(str(error)) from None
