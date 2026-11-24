import json
from datetime import UTC, date, datetime, timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from .historical_backfill import make_plan
from .historical_calendars import CalendarError, load_calendar
from .historical_data import BarInput, store_bars
from .historical_fx import FXInput, HistoricalFXUnavailable, fx_available_at, historical_fx_rate, store_fx_rates
from .historical_providers import instrument_identity
from .models import HistoricalBar, HistoricalFXRate

DAY = timedelta(days=1)


class ExchangeCalendarTests(TestCase):
    def test_us_holidays_special_closure_and_short_session(self):
        for mic in ("XNYS", "XNAS"):
            calendar = load_calendar("stock", mic)
            self.assertFalse(calendar.is_session(date(2025, 1, 9)))
            self.assertFalse(calendar.is_session(date(2025, 7, 4)))
            self.assertEqual(calendar.close_at(date(2025, 12, 24)).hour, 18)
            self.assertEqual(calendar.close_at(date(2025, 3, 7)).hour, 21)
            self.assertEqual(calendar.close_at(date(2025, 3, 10)).hour, 20)

    def test_euronext_holidays_short_session_and_dst(self):
        for mic in ("XAMS", "XBRU"):
            calendar = load_calendar("etf", mic)
            self.assertFalse(calendar.is_session(date(2025, 4, 21)))
            self.assertFalse(calendar.is_session(date(2025, 5, 1)))
            self.assertEqual(calendar.close_at(date(2025, 12, 24)).strftime("%H:%M"), "13:05")
            self.assertEqual(calendar.close_at(date(2025, 3, 28)).hour, 16)
            self.assertEqual(calendar.close_at(date(2025, 3, 31)).hour, 15)

    def test_crypto_weekends_and_unsupported_mic_or_year(self):
        calendar = load_calendar("crypto", "BINANCE")
        saturday = date(2025, 12, 27)
        self.assertEqual(calendar.close_at(saturday), datetime(2025, 12, 28, tzinfo=UTC))
        self.assertTrue(calendar.is_session(saturday))
        with self.assertRaises(CalendarError):
            load_calendar("stock", "XXXX")
        with self.assertRaises(CalendarError):
            load_calendar("stock", "XNAS").is_session(date(1999, 12, 31))


class ReadinessTests(TestCase):
    def setUp(self):
        self.read = self.enterContext(patch("trading.market_data.read_json"))
        self.read.side_effect = AssertionError("Provider I/O is unexpected")
        self.args = [
            "--provider", "twelve", "--asset-class", "stock", "--symbol", "ABC",
            "--exchange", "XNAS", "--currency", "USD", "--start", "2025-12-23", "--end", "2025-12-29",
        ]
        self.calendar = load_calendar("stock", "XNAS")
        self.plan = make_plan(self.calendar, start=date(2025, 12, 23), end=date(2025, 12, 29))
        self.identity = instrument_identity("twelve", "stock", "ABC", "XNAS", "USD", "raw")

    def report(self, *args):
        output = StringIO()
        call_command("historical_data_readiness", *(args or self.args), stdout=output)
        return json.loads(output.getvalue())

    def store_ohlcv(self, *exclude):
        store_bars(**self.identity, calendar=self.calendar, bars=[
            BarInput(day, self.calendar.close_at(day), 100, 101, 99, 100)
            for day in self.plan.sessions if day not in exclude
        ])

    def test_read_only_report_exposes_partial_and_then_complete_ohlcv_and_fx(self):
        missing = date(2025, 12, 24)
        self.store_ohlcv(missing)
        rejected = date(2025, 12, 26)
        HistoricalBar.objects.filter(session_date=rejected).update(quality=HistoricalBar.Quality.SPIKE)
        with self.assertNumQueries(2):
            report = self.report()
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["ohlcv"]["gap_dates"], [missing.isoformat()])
        self.assertEqual(report["ohlcv"]["rejected_dates"], [rejected.isoformat()])
        self.assertTrue(report["ohlcv"]["warmup_ready"])
        self.assertEqual(report["fx"]["availability"], "modeled")
        self.assertEqual(report["fx"]["covered_valuations"], 0)
        self.assertEqual(report["calendar"]["source"], "exchange_calendars")
        HistoricalBar.objects.filter(session_date=rejected).update(quality=HistoricalBar.Quality.VALID)
        self.store_ohlcv()
        fx_days = {date.fromisoformat(row["fx_date"]) for row in report["fx"]["gaps"]}
        store_fx_rates(FXInput(day, "1.25") for day in fx_days)
        self.assertEqual(self.report()["status"], "ready")
        self.read.assert_not_called()

    def test_open_and_future_sessions_are_excluded_from_report_and_plan(self):
        args = self.args.copy()
        args[-1] = "2025-12-30"
        args[args.index("USD")] = "EUR"
        with patch("trading.historical_backfill.timezone.now",
                   return_value=datetime(2025, 12, 29, 16, tzinfo=UTC)):
            report = self.report(*args)
        self.assertEqual(report["end"], "2025-12-28")
        self.assertEqual(report["requested_end"], "2025-12-30")
        self.assertEqual(report["ohlcv"]["gap_dates"], ["2025-12-23", "2025-12-24", "2025-12-26"])
        self.assertEqual(report["fx"]["status"], "not_required")
        self.read.assert_not_called()

    def test_unknown_calendar_stops_before_database_and_currency_is_separate(self):
        args = self.args.copy()
        args[args.index("XNAS")] = "XXXX"
        with self.assertNumQueries(0), self.assertRaisesMessage(CommandError, "Calendar unreliable"):
            self.report(*args)
        args[args.index("XXXX")] = "XNAS"
        args[args.index("USD")] = "GBP"
        self.assertEqual(self.report(*args)["fx"]["status"], "unsupported_currency")
        self.read.assert_not_called()

    @patch.dict("os.environ", {"TWELVE_DATA_API_KEY": "fixture-only"})
    def test_automatic_backfill_and_rerun_use_same_schedule(self):
        payload = {
            "meta": {"symbol": "ABC", "mic_code": "XNAS", "currency": "USD", "interval": "1day"},
            "values": [
                {"datetime": day.isoformat(), "open": 100, "high": 101, "low": 99, "close": 100}
                for day in self.plan.sessions
            ],
        }
        self.read.side_effect = None
        self.read.return_value = payload
        results = []
        for _ in range(2):
            output = StringIO()
            call_command("backfill_historical_data", *self.args, "--apply", stdout=output)
            results.append(json.loads(output.getvalue().splitlines()[-1]))
        self.assertTrue(results[0]["coverage"]["covered"])
        self.assertEqual(results[0]["inserted"], len(self.plan.sessions))
        self.assertEqual(results[1]["requests"], 0)
        self.assertEqual(self.read.call_count, 1)


class FXCalendarPolicyTests(TestCase):
    @override_settings(HISTORICAL_FX_HOLIDAYS="2025-12-25")
    def test_configured_holiday_skips_source_session_and_preserves_prior_rate(self):
        wed, thu, fri = date(2025, 12, 24), date(2025, 12, 25), date(2025, 12, 26)
        result = store_fx_rates([FXInput(wed, "1.25"), FXInput(thu, "1.5"), FXInput(fri, "2")])
        self.assertEqual((result["inserted"], result["rejected"]), (2, 1))
        self.assertEqual(historical_fx_rate("EUR", "USD", as_of=fx_available_at(thu)), 1.25)

    def test_new_policy_does_not_make_existing_rate_available_earlier(self):
        day = date(2025, 12, 24)
        store_fx_rates([FXInput(day, "1.25")])
        original = HistoricalFXRate.objects.get().available_at
        with override_settings(HISTORICAL_FX_PUBLICATION_DELAY_MINUTES=0):
            with self.assertRaisesMessage(HistoricalFXUnavailable, "unavailable_at_valuation"):
                historical_fx_rate("EUR", "USD", as_of=original - timedelta(minutes=1))
            self.assertEqual(store_fx_rates([FXInput(day, "1.25")])["duplicates"], 1)
            self.assertEqual(HistoricalFXRate.objects.get().available_at, original)
        with override_settings(HISTORICAL_FX_MAX_AGE_HOURS=1):
            with self.assertRaisesMessage(HistoricalFXUnavailable, "maximum age"):
                historical_fx_rate("EUR", "USD", as_of=original + timedelta(hours=2))
