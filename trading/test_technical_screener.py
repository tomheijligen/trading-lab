import json
from datetime import UTC, datetime, timedelta
from math import log
from statistics import pstdev
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from .historical_calendars import load_calendar
from .historical_data import expected_sessions
from .models import Account, HistoricalBar, Instrument
from .technical_screener import technical_rows, technical_watchlist

NOW = datetime(2025, 12, 29, 12, tzinfo=UTC)
END = NOW.date() - timedelta(days=1)


def entry(symbol="BTC/EUR", group="crypto", exchange="BINANCE", provider="binance", currency="EUR", adjustment="raw"):
    return {
        "symbol": symbol, "group": group, "exchange": exchange,
        "provider": provider, "currency": currency, "adjustment": adjustment,
    }


def make_bar(identity, day, close, volume=10, **overrides):
    calendar = load_calendar(identity["asset_class"], identity["exchange"])
    return HistoricalBar(**{
        **identity, "session_date": day, "observed_at": calendar.close_at(day), "open": close,
        "high": close + 2, "low": close - 2, "close": close, "volume": volume, **overrides,
    })


@override_settings(PLATFORM_LAB_ENABLED=True)
class TechnicalScreenerTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("technical-fixture")
        self.enterContext(patch("trading.technical_screener.timezone.now", return_value=NOW))
        self.provider = self.enterContext(
            patch("trading.market_data.read_json", side_effect=AssertionError("Unexpected provider request"))
        )
        self.url = reverse("technical_scanner")

    def watch(self, *entries):
        self.enterContext(patch.dict("os.environ", {"MARKET_SCANNER_UNIVERSE": json.dumps(entries)}))
        return technical_watchlist()

    def seed(self, row, count, *, end=END):
        identity = row["series"]
        calendar = load_calendar(identity["asset_class"], identity["exchange"])
        dates = expected_sessions(end - timedelta(days=400), end, calendar)[-count:]
        HistoricalBar.objects.bulk_create([
            make_bar(identity, day, 100 + index, 20 if index == count - 1 else 10)
            for index, day in enumerate(dates)
        ])
        return dates

    def test_linear_prices_produce_known_indicator_values_in_one_query(self):
        watch = self.watch(entry())
        self.seed(watch[0], 364)
        with self.assertNumQueries(1):
            metrics = technical_rows(watch)[0]["metrics"]
        expected = {
            "price": 463, "volume": 20, "relative_volume": 2,
            "sma20": 453.5, "sma50": 438.5, "sma200": 363.5,
            "rsi": 100, "macd": 7, "macd_signal": 7, "macd_histogram": 0, "atr": 4,
            "return_10": (463 / 453 - 1) * 100, "return_20": (463 / 443 - 1) * 100,
            "trend": (463 / 453.5 - 1) * 100, "range": 400 / 463,
            "volatility": pstdev(log((price + 1) / price) for price in range(443, 463)) * 100,
            "high_52": 465, "low_52": 98,
        }
        for key, value in expected.items():
            with self.subTest(key=key):
                self.assertAlmostEqual(metrics[key].value, value)
                self.assertEqual((metrics[key].status, metrics[key].as_of, metrics[key].source),
                                 ("available", END, "Binance"))
        self.assertEqual(metrics["volume"].unit, "BTC")

    def test_missing_volume_gap_and_rejected_latest_bar(self):
        watch = self.watch(entry())
        days = self.seed(watch[0], 200)
        HistoricalBar.objects.filter(session_date=days[0]).delete()
        HistoricalBar.objects.filter(session_date=days[-1]).update(volume=None)
        metrics = technical_rows(watch)[0]["metrics"]
        self.assertEqual(metrics["sma200"].status, "insufficient")
        self.assertEqual(metrics["volume"].status, "missing")
        self.assertIsNone(metrics["relative_volume"].value)
        HistoricalBar.objects.filter(session_date=days[-2]).delete()
        self.assertIsNone(technical_rows(watch)[0]["metrics"]["sma20"].value)
        HistoricalBar.objects.filter(session_date=days[-1]).update(quality="spike")
        price = technical_rows(watch)[0]["metrics"]["price"]
        self.assertEqual((price.as_of, price.status), (days[-3], "stale"))

    def test_provider_venue_currency_adjustment_and_symbol_never_fill_gaps(self):
        watch = self.watch(entry("AAA", "stock", "XNYS", "twelve", "USD", "split"))
        identity = watch[0]["series"]
        day = datetime(2025, 12, 26).date()
        alternatives = [
            {"provider": "Alpaca IEX"}, {"exchange": "XNAS"}, {"currency": "EUR"},
            {"adjustment": "raw"}, {"symbol": "BBB"}, {"asset_class": "etf"},
        ]
        HistoricalBar.objects.bulk_create([make_bar({**identity, **change}, day, 999) for change in alternatives])
        self.assertIsNone(technical_rows(watch)[0]["metrics"]["price"].value)
        self.seed(watch[0], 21)
        metrics = technical_rows(watch)[0]["metrics"]
        self.assertEqual((metrics["price"].as_of, metrics["price"].status, metrics["price"].value),
                         (day, "available", 120))
        self.assertIsNotNone(metrics["sma20"].value)

    def test_form_combines_filters_and_sorting_preserves_ties(self):
        watch = self.watch(entry("AAA/EUR"), entry("BBB/EUR"), entry("CCC/EUR"), entry("DDD/EUR"))
        self.seed(watch[0], 21)
        self.seed(watch[1], 21)
        self.seed(watch[2], 20)
        self.client.force_login(self.user)
        response = self.client.get(self.url, {
            "asset_class": "crypto", "exchange": "BINANCE", "currency": "EUR", "price_min": 119,
            "sma20_max": 111, "rsi_min": 80, "volume_min": 15, "sort": "price", "direction": "desc",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["symbol"] for row in response.context["technical_rows"]],
                         ["AAA/EUR", "BBB/EUR", "CCC/EUR"])
        self.assertContains(response, "2025-12-28")
        self.assertContains(response, "Insufficient history")
        self.assertContains(self.client.get(self.url, {"price_min": 10}), "Choose a currency")
        self.provider.assert_not_called()

    def test_access_flags_original_screener_and_learning_lab(self):
        self.watch(entry())
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.client.force_login(self.user)
        with override_settings(PLATFORM_LAB_ENABLED=False):
            self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.post(self.url).status_code, 405)
        self.assertContains(self.client.get(reverse("market_scanner")), "Update ranking")
        account = Account.objects.create(name="Fixture", owner=self.user, mode="virtual")
        Instrument.objects.create(account=account, symbol="BTC/EUR", group="crypto", history=[
            [(END - timedelta(days=120-index)).isoformat(), str(100 + index)] for index in range(120)
        ])
        response = self.client.get(reverse("strategy_comparison"), {"learn": account.pk})
        self.assertEqual(len(response.context["learning"]["training"]), 10)
        self.provider.assert_not_called()

    def test_open_session_unknown_calendar_and_legacy_configuration(self):
        watch = self.watch(
            entry(), entry("AAA", "stock", "XETR", "twelve", "EUR", "split"),
            {"group": "stock", "symbol": "LEGACY", "exchange": "XNAS"},
        )
        crypto = next(row for row in watch if row["symbol"] == "BTC/EUR")
        self.seed(crypto, 21)
        today = NOW.date()
        HistoricalBar.objects.create(**crypto["series"], session_date=today, observed_at=NOW,
                                     open=999, high=999, low=999, close=999)
        rows = {row["symbol"]: row for row in technical_rows(watch)}
        self.assertEqual(rows["BTC/EUR"]["metrics"]["price"].as_of, END)
        self.assertEqual(rows["AAA"]["status"], "unknown_calendar")
        self.assertEqual(rows["LEGACY"]["status"], "unconfigured")
