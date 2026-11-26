import json

from django.core.management.base import BaseCommand, CommandError

from trading.historical_readiness import inspect_readiness
from trading.management.historical import add_series_arguments, resolve_series


class Command(BaseCommand):
    help = "Read historical OHLCV and FX readiness for one series without provider requests or writes."

    def add_arguments(self, parser):
        add_series_arguments(parser)

    def handle(self, *args, **options):
        identity, calendar, plan = resolve_series(options)
        try:
            self.stdout.write(json.dumps(inspect_readiness(identity, calendar, plan)))
        except ValueError as error:
            raise CommandError(str(error)) from None
