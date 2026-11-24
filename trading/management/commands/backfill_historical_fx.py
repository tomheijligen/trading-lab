import json
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from trading.historical_fx import FX_SERIES
from trading.historical_fx_backfill import backfill_fx, plan_fx_sessions


class Command(BaseCommand):
    help = "Plan daily historical FX offline. Use --apply to authorize provider requests and writes."

    def add_arguments(self, parser):
        parser.add_argument("--pair", choices=("EUR/USD", "USD/EUR"), default="EUR/USD")
        parser.add_argument("--start", type=date.fromisoformat, required=True)
        parser.add_argument("--end", type=date.fromisoformat, required=True)
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true", help="Offline estimate; the default.")
        mode.add_argument("--apply", action="store_true", help="Authorize provider requests and FX writes.")

    def handle(self, *args, **options):
        start, end = options["start"], options["end"]
        try:
            sessions = plan_fx_sessions(start, end)
        except ValueError as error:
            raise CommandError(str(error)) from None
        self.stdout.write(json.dumps({
            "mode": "apply" if options["apply"] else "dry-run",
            "requested_pair": options["pair"], "series": FX_SERIES,
            "start": start.isoformat(), "end": end.isoformat(),
            "expected_sessions": len(sessions), "estimated_credits_before_cache_and_retry": 1,
        }))
        if options["apply"]:
            result, failure = backfill_fx(start, end, sessions)
            self.stdout.write(json.dumps(result))
            if failure:
                raise CommandError(f"Historical FX backfill stopped. {failure}")
