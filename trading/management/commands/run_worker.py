import logging
import signal
from pathlib import Path
from threading import Event
from time import monotonic

from django.conf import settings
from django.core.management import BaseCommand, call_command
from django.db import close_old_connections

from trading.backtesting import process_simulation

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run supervised paper rounds or queued historical simulations."

    def add_arguments(self, parser):
        parser.add_argument("role", choices=("paper", "simulation"))
        parser.add_argument("--heartbeat", type=Path)
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, role, heartbeat, once, **options):
        stopped = Event()
        previous = {}

        def stop(_number, _frame):
            stopped.set()

        for number in (signal.SIGTERM, signal.SIGINT):
            previous[number] = signal.signal(number, stop)

        try:
            interval = settings.MARKET_REFRESH_MINUTES * 60 if role == "paper" else 5
            while not stopped.is_set():
                started = monotonic()
                try:
                    close_old_connections()
                    if role == "paper":
                        call_command("run_bots", stdout=self.stdout, stderr=self.stderr)
                        if settings.PLATFORM_LAB_ENABLED:
                            call_command("refresh_scanner", stderr=self.stderr)
                    elif settings.PLATFORM_LAB_ENABLED:
                        process_simulation()
                    if heartbeat:
                        heartbeat.touch()
                except Exception:
                    if once:
                        raise
                    logger.exception("Background %s cycle failed", role)
                finally:
                    close_old_connections()
                if once:
                    return
                stopped.wait(max(1, interval - (monotonic() - started)))
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
