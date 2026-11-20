from django.core.management.base import BaseCommand

from trading.scanner import refresh_scanner


class Command(BaseCommand):
    help = "Refresh the configured global market scanner watchlist."

    def handle(self, *args, **options):
        for error in refresh_scanner():
            self.stderr.write(error)
