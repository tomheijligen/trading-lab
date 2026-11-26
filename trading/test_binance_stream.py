from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from types import SimpleNamespace

from .binance_stream import latest_stream_candle, save_candle
from .engine import binance_snapshot


class BinanceStreamTests(TestCase):
    def event(self, timestamp, close="105", closed=True):
        return {"data": {"k": {
            "s": "BTCEUR", "i": "1m", "x": closed,
            "T": int(timestamp.timestamp() * 1000) - 1, "c": close,
        }}}

    def test_only_recent_completed_monotonic_candles_are_used(self):
        now = timezone.now()
        symbols = {"BTCEUR"}
        self.assertFalse(save_candle(self.event(now, closed=False), symbols))
        self.assertFalse(save_candle(self.event(now - timedelta(minutes=5)), symbols))
        self.assertTrue(save_candle(self.event(now - timedelta(seconds=1)), symbols))
        self.assertFalse(save_candle(self.event(now - timedelta(minutes=1)), symbols))
        self.assertEqual(latest_stream_candle("BTCEUR")[1], Decimal("105"))

    @override_settings(MARKET_BINANCE_STREAM_SYMBOLS={"BTCEUR"})
    @patch("trading.engine.BINANCE.fetch")
    def test_stream_replaces_minute_rest_request(self, fetch):
        now = timezone.now()
        day = now - timedelta(days=2)
        fetch.return_value = [[int(day.timestamp() * 1000), 0, 0, 0, "100", 0,
                               int((day + timedelta(days=1)).timestamp() * 1000)]]
        save_candle(self.event(now - timedelta(seconds=1)), {"BTCEUR"})
        item = SimpleNamespace(symbol="BTC/EUR")
        snapshot = binance_snapshot(item, {"EUR": Decimal(1)})
        self.assertEqual(snapshot.price, Decimal("105"))
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args[1]["interval"], "1d")
