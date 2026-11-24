import json

from django.core.management.base import BaseCommand, CommandError

from trading.historical_backfill import estimate_requests, execute_backfill
from trading.historical_data import WARMUP_SESSIONS
from trading.management.historical import add_series_arguments, resolve_series


class Command(BaseCommand):
    help = "Plan daily OHLCV backfill offline. Use --apply to authorize provider requests and writes."

    def add_arguments(self, parser):
        add_series_arguments(parser)
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true", help="Offline estimate; the default.")
        mode.add_argument("--apply", action="store_true", help="Explicit confirmation of provider requests and writes.")

    def handle(self, *args, **options):
        provider = options["provider"]
        identity, calendar, plan = resolve_series(options)

        estimated, maximum, credits = estimate_requests(provider, plan.windows)
        self.stdout.write(json.dumps({
            "mode": "apply" if options["apply"] else "dry-run", "series": identity,
            "start": plan.start.isoformat(), "end": plan.end.isoformat(),
            "requested_end": plan.requested_end.isoformat(), "calendar_source": calendar.source,
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
