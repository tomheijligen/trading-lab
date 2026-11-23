import json
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from trading.historical_backfill import estimate_requests, execute_backfill, make_plan
from trading.historical_calendars import CalendarError, load_calendar
from trading.historical_data import SIMULATION_WINDOWS, WARMUP_SESSIONS
from trading.historical_providers import PROVIDERS, TWELVE_ADJUSTMENTS, instrument_identity


class Command(BaseCommand):
    help = "Plan daily OHLCV backfill offline. Use --apply to authorize provider requests and writes."

    def add_arguments(self, parser):
        parser.add_argument("--provider", required=True, choices=PROVIDERS)
        parser.add_argument("--asset-class", required=True, choices=("stock", "etf", "crypto"))
        parser.add_argument("--symbol", required=True)
        parser.add_argument("--exchange", required=True, help="Listing MIC for equities or venue for crypto.")
        parser.add_argument("--currency", required=True, help="Source or quote currency.")
        parser.add_argument("--adjustment", choices=TWELVE_ADJUSTMENTS, default="raw")
        parser.add_argument("--calendar-file", help="Complete venue schedule covering the period and warm-up.")
        period = parser.add_mutually_exclusive_group()
        period.add_argument("--start", type=date.fromisoformat)
        period.add_argument("--days", type=int, choices=SIMULATION_WINDOWS)
        parser.add_argument("--end", type=date.fromisoformat, help="Inclusive end; defaults to yesterday at the venue.")
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true", help="Offline estimate; the default.")
        mode.add_argument("--apply", action="store_true", help="Explicit confirmation of provider requests and writes.")

    def handle(self, *args, **options):
        provider = options["provider"]
        try:
            identity = instrument_identity(provider, **{
                field: options[field] for field in ("asset_class", "symbol", "exchange", "currency", "adjustment")
            })
            calendar = load_calendar(identity["asset_class"], identity["exchange"], options["calendar_file"])
            if provider == "alpaca" and calendar.timezone.key != "America/New_York":
                raise CalendarError("Calendar unreliable: Alpaca US equity sessions require America/New_York.")
            plan = make_plan(calendar, end=options["end"], start=options["start"], days=options["days"])
        except ValueError as error:
            raise CommandError(str(error)) from None

        estimated, maximum, credits = estimate_requests(provider, plan.windows)
        self.stdout.write(json.dumps({
            "mode": "apply" if options["apply"] else "dry-run", "series": identity,
            "start": plan.start.isoformat(), "end": plan.end.isoformat(),
            "fetch_start": plan.sessions[0].isoformat(),
            "expected_sessions": len(plan.sessions) - WARMUP_SESSIONS,
            "warmup_sessions": WARMUP_SESSIONS, "calendar_status": "checked",
            "estimated_requests": estimated, "maximum_pages_without_retries": maximum,
            "estimated_quota_units": credits,
        }))
        if not options["apply"]:
            return

        result, failure = execute_backfill(provider, identity, calendar, plan)
        self.stdout.write(json.dumps(result))
        if failure:
            raise CommandError(f"Backfill stopped; rerun with the same arguments to resume. {failure}")
