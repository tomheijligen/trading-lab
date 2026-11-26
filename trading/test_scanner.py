from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import MarketObservation
from .scanner import rank_observations


@override_settings(PLATFORM_LAB_ENABLED=True)
class ScannerTests(TestCase):
    def test_ranking_excludes_unlisted_symbols_and_nominal_price(self):
        now = timezone.now()
        for symbol, price in (("CHEAP", 1), ("EXPENSIVE", 1000), ("PRIVATE", 10)):
            MarketObservation.objects.create(
                group="etf", symbol=symbol, requested_exchange="X", exchange="X",
                provider="Test", currency="EUR", price_eur=price, quote_time=now,
                received_at=now, market_open=True,
                history=[
                    [(now.date() - timedelta(days=21 - i)).isoformat(),
                     str(Decimal(price) * (100 + i) / 100)]
                    for i in range(21)
                ],
            )
        results = rank_observations([("etf", "CHEAP", "X"), ("etf", "EXPENSIVE", "X")], (1, 1, 1))
        self.assertEqual({row["row"].symbol for row in results}, {"CHEAP", "EXPENSIVE"})
        self.assertAlmostEqual(results[0]["score"], results[1]["score"])

    @patch.dict("os.environ", {"MARKET_SCANNER_UNIVERSE": "[]"})
    def test_provider_usage_is_staff_only(self):
        user = get_user_model().objects.create_user("scanner")
        self.client.force_login(user)
        self.assertContains(self.client.get(reverse("market_scanner")), "Update ranking")
        url = reverse("market_providers")
        self.assertEqual(self.client.get(url).status_code, 404)
        user.is_staff = True
        user.save(update_fields=["is_staff"])
        self.assertContains(self.client.get(url), "Twelve Data")
