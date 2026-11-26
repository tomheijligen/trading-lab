import json
import logging
import re
import signal
from threading import Event

from django.conf import settings
from django.core.management import BaseCommand, CommandError
from django.db import close_old_connections

from trading.binance_stream import save_candle

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Persist completed public Binance spot minute candles."

    def handle(self, *args, **options):
        from websocket import WebSocketApp

        if not settings.MARKET_BINANCE_ENABLED:
            raise CommandError("Enable MARKET_BINANCE_ENABLED for this worker.")
        symbols = settings.MARKET_BINANCE_STREAM_SYMBOLS
        if not 1 <= len(symbols) <= 50 or any(not re.fullmatch(r"[A-Z0-9]{4,24}", s) for s in symbols):
            raise CommandError("Configure 1 to 50 uppercase Binance spot pairs, for example BTCEUR,ETHEUR.")
        streams = "/".join(f"{symbol.lower()}@kline_1m" for symbol in sorted(symbols))
        url = f"wss://data-stream.binance.vision/stream?streams={streams}"
        stopped = Event()

        def receive(_socket, raw):
            try:
                message = json.loads(raw)
            except ValueError:
                return
            close_old_connections()
            save_candle(message, symbols)

        def error(_socket, failure):
            logger.warning("Binance stream error: %s", type(failure).__name__)

        while not stopped.is_set():
            client = WebSocketApp(url, on_message=receive, on_error=error)

            def stop(*_args):
                stopped.set()
                client.close()

            signal.signal(signal.SIGINT, stop)
            signal.signal(signal.SIGTERM, stop)
            client.run_forever(ping_interval=30, ping_timeout=10)
            if not stopped.is_set():
                logger.warning("Binance stream closed; reconnecting in 15 seconds")
                stopped.wait(15)
