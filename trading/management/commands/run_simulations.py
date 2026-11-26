from django.core.management.base import BaseCommand

from trading.backtesting import process_simulation


class Command(BaseCommand):
    help = "Process one queued historical simulation."

    def handle(self, *args, **options):
        self.stdout.write("Processed one simulation." if process_simulation() else "No queued simulation.")
