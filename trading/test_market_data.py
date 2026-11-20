from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from .engine import Snapshot, market_snapshot
from .market_data import DataError, Provider
from .models import MarketBar, ProviderUsage


class MarketDataTests(TestCase):
    @patch("trading.market_data.read_json", return_value={"price": "100"})
    def test_persistent_cache_and_weighted_budget(self, read_json):
        provider = Provider("Test provider", "https://example.invalid/", 2, 2)
        ttl = timedelta(minutes=5)
        for _ in range(2):
            self.assertEqual(
                provider.fetch("quote", {"symbol": "A"}, {}, ttl=ttl, weight=2),
                {"price": "100"},
            )
        with self.assertRaises(DataError):
            provider.fetch("quote", {"symbol": "B"}, {}, ttl=ttl)
        self.assertEqual(read_json.call_count, 1)
        self.assertEqual(ProviderUsage.objects.get().day_credits, 2)

    @patch("trading.market_data.read_json", side_effect=TimeoutError)
    def test_failed_request_is_charged_and_temporarily_suppressed(self, read_json):
        provider = Provider("Test provider", "https://example.invalid/", 2)
        for _ in range(2):
            with self.assertRaises(DataError):
                provider.fetch("quote", {}, {}, ttl=timedelta(minutes=1))
        self.assertEqual(read_json.call_count, 1)
        self.assertIsNotNone(ProviderUsage.objects.get().last_failure)

    @override_settings(MARKET_BINANCE_ENABLED=True)
    @patch("trading.engine.binance_snapshot")
    @patch("trading.engine.twelve_snapshot")
    def test_stale_primary_quote_falls_back_and_history_is_deduplicated(self, twelve, binance):
        now = timezone.now()
        history = [[(now.date() - timedelta(days=1)).isoformat(), "100"]]
        twelve.return_value = Snapshot(history, "EUR", now - timedelta(hours=1), Decimal(100), True)
        binance.return_value = Snapshot(
            history, "EUR", now, Decimal(100), True, provider="Binance", exchange="Binance",
        )
        item = SimpleNamespace(symbol="BTC/EUR", exchange="", group="crypto")
        for _ in range(2):
            self.assertEqual(market_snapshot(item, {}).provider, "Binance")
        self.assertEqual(MarketBar.objects.count(), 1)
