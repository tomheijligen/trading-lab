from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from .backtesting import calculate, process_simulation
from .historical_calendars import load_calendar
from .historical_data import BarInput, store_bars
from .models import Account, HistoricalBar, Instrument, Simulation, Trade
from .simulation_data import OHLCV_FILL_MODEL


@override_settings(PLATFORM_LAB_ENABLED=True)
class OHLCVSimulationTests(TestCase):
    def setUp(self):
        self.enterContext(patch("trading.market_data.read_json", side_effect=AssertionError("Provider I/O")))
        self.user = get_user_model().objects.create_user("ohlcv-research")
        self.account = Account.objects.create(owner=self.user, mode="virtual", name="Fixture", cash=5000)
        self.client.force_login(self.user)
        self.url = reverse("simulations", args=[self.account.pk])
        self.start = date(2025, 12, 27)
        self.end = self.start + timedelta(days=2)
        self.calendar = load_calendar("crypto", "BINANCE")
        self.identity = {
            "provider": "Binance", "asset_class": "crypto", "symbol": "BTC/EUR",
            "exchange": "BINANCE", "currency": "EUR", "adjustment": "raw",
        }
        self.data = {
            **self.identity, "source": "ohlcv", "provider": "binance",
            "start": self.start.isoformat(), "end": self.end.isoformat(), "capital": "5000",
            "fee_bps": 20, "spread_bps": 10, "slippage_bps": 5,
        }

    def seed(self, *, omit=None, changes=None):
        bars = []
        for offset in range(-30, 3):
            day = self.start + timedelta(days=offset)
            if day == omit:
                continue
            opening = 80 if offset == 1 else 100
            close = 103 if offset == 0 else 100
            bars.append(BarInput(day, self.calendar.close_at(day), opening, max(opening, close),
                                 min(opening, close), close, 10))
        store_bars(**{**self.identity, **(changes or {})}, bars=bars, calendar=self.calendar)

    def queue(self):
        response = self.client.post(self.url, self.data)
        self.assertEqual(response.status_code, 302, response.content[:300])
        return Simulation.objects.get(account=self.account)

    def test_complete_series_queues_and_exports_with_no_trading_writes(self):
        self.seed()
        instrument = Instrument.objects.create(account=self.account, symbol="BTC/EUR", group="crypto", units=2)
        Trade.objects.create(instrument=instrument, side="Buy", bot="trend", price=100, units=2, fee=1)
        models = (Account, Instrument, Trade, HistoricalBar)
        before = [list(model.objects.order_by("pk").values()) for model in models]
        job = self.queue()
        self.assertEqual(len(job.inputs["bars"]), 33)
        self.assertIn(self.start.isoformat(), job.inputs["bars"])
        expected = calculate(job)
        self.assertTrue(process_simulation())
        job.refresh_from_db()
        self.assertEqual(job.result, expected)
        exported = self.client.get(reverse("simulation_export", args=[self.account.pk, job.pk])).json()
        self.assertEqual(exported["instruments"], ["BTC/EUR"])
        self.assertEqual(exported["results"]["historical_data"]["fill_model"], OHLCV_FILL_MODEL)
        self.assertEqual(before, [list(model.objects.order_by("pk").values()) for model in models])

    def test_coverage_refuses_missing_rejected_and_wrong_series(self):
        self.seed(omit=self.start)
        for changes in ({"provider": "Twelve Data"}, {"symbol": "ETH/EUR"},
                        {"adjustment": "split"}, {"exchange": "OTHER"}, {"currency": "USD"}):
            self.seed(changes=changes)
        response = self.client.post(self.url, self.data)
        self.assertContains(response, "1 missing")
        self.seed()
        HistoricalBar.objects.filter(**self.identity, session_date=self.start).update(
            quality=HistoricalBar.Quality.SPIKE,
        )
        self.assertContains(self.client.post(self.url, self.data), "1 rejected")
        self.assertFalse(Simulation.objects.exists())

    def test_missing_warmup_and_non_eur_input_are_refused(self):
        self.seed(omit=self.start - timedelta(days=1))
        self.assertContains(self.client.post(self.url, self.data), "29/30")
        self.assertContains(self.client.post(self.url, {**self.data, "currency": "USD"}), "Select a valid choice")
        self.assertFalse(Simulation.objects.exists())

    def test_late_candle_cannot_be_used_for_a_session_close_mark(self):
        self.seed()
        HistoricalBar.objects.filter(**self.identity, session_date=self.start).update(
            observed_at=self.calendar.close_at(self.start) + timedelta(minutes=1),
        )
        self.assertContains(self.client.post(self.url, self.data), "not usable at session close")
        self.assertFalse(Simulation.objects.exists())

    def test_corrupt_stored_crypto_snapshot_with_a_gap_is_rejected(self):
        self.seed()
        job = self.queue()
        del job.inputs["bars"][(self.start + timedelta(days=1)).isoformat()]
        job.save(update_fields=["inputs"])
        self.assertTrue(process_simulation())
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")

    def test_strict_open_timing_and_execution_costs(self):
        self.seed()
        job = self.queue()
        with patch("trading.backtesting.signal_for", return_value=("buy", "")) as signal:
            result = calculate(job)
        # Saturday cannot trade: Friday's close and Saturday's open share a timestamp.
        # Sunday's decision excludes Saturday's 103 close for the same reason.
        self.assertEqual(signal.call_args_list[0].args[1], [Decimal(100)] * 30)
        self.assertEqual(signal.call_args_list[1].args[1][-1], Decimal(103))
        row = result["strategies"][0]
        self.assertEqual(row["equity"][1], [self.start.isoformat(), 5000])
        gross = Decimal(5000) / Decimal("1.002")
        units = gross / (Decimal(80) * Decimal("1.001"))
        expected = units * Decimal(100) * Decimal(".999") * Decimal(".998")
        self.assertAlmostEqual(row["equity"][2][1], float(expected))
        self.assertAlmostEqual(row["fees"], float(gross * Decimal(".002")))

    def test_queue_uses_snapshot_if_source_changes_and_old_form_still_queues(self):
        self.seed()
        job = self.queue()
        expected = calculate(job)
        HistoricalBar.objects.filter(**self.identity, session_date=self.start).update(close=101)
        self.assertEqual(calculate(job), expected)
        Instrument.objects.create(
            account=self.account, symbol="BTC/EUR", group="crypto", currency="EUR",
            history=[[(self.start + timedelta(days=offset)).isoformat(), "100"] for offset in range(-30, 3)],
        )
        legacy = {field: self.data[field] for field in ("start", "end", "capital", "fee_bps", "spread_bps", "slippage_bps")}
        self.assertEqual(self.client.post(self.url, legacy).status_code, 302)
        self.assertTrue(process_simulation())
        self.assertTrue(process_simulation())
        self.assertEqual(Simulation.objects.filter(status="done").count(), 2)
        self.assertNotIn("historical_data", Simulation.objects.latest("pk").result)
