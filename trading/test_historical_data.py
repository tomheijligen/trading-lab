from datetime import date, datetime, timedelta, UTC
from decimal import Decimal

from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from .historical_data import (
    BarInput, WeekdayCalendar, expected_sessions, inspect_coverage, store_bars, validate_bars,
)
from .models import HistoricalBar


class HistoricalDataTests(TestCase):
    def setUp(self):
        self.identity = {
            "provider": "Fixture", "asset_class": "stock", "symbol": "ABC",
            "exchange": "NASDAQ", "currency": "USD", "adjustment": "split",
        }
        self.today = date(2025, 12, 31)
        self.calendar = WeekdayCalendar(frozenset({date(2025, 12, 25)}))

    def bar(self, day, close, *, opened=None, high=None, low=None, volume=100):
        close = Decimal(str(close))
        opened = close if opened is None else Decimal(str(opened))
        return BarInput(
            day, datetime.combine(day, datetime.min.time(), tzinfo=UTC),
            opened, max(opened, close) if high is None else Decimal(str(high)),
            min(opened, close) if low is None else Decimal(str(low)),
            close, volume,
        )

    def test_ohlcv_storage_preserves_source_currency_adjustment_and_is_idempotent(self):
        bars = [self.bar(self.today - timedelta(days=2), "100"),
                self.bar(self.today - timedelta(days=1), "102")]
        first = store_bars(**self.identity, bars=bars)
        second = store_bars(**self.identity, bars=bars)
        row = HistoricalBar.objects.get(session_date=self.today - timedelta(days=1))
        self.assertEqual(first["inserted"], 2)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["duplicates"], 2)
        self.assertEqual(row.currency, "USD")
        self.assertEqual(row.adjustment, "split")
        self.assertEqual(row.open, Decimal("102"))
        self.assertEqual(row.volume, Decimal("100"))

    def test_source_duplicates_invalid_prices_and_spikes_are_flagged(self):
        day = self.today - timedelta(days=7)
        result = validate_bars([
            self.bar(day, "100"), self.bar(day, "101"),
            self.bar(day + timedelta(days=1), "0"),
            self.bar(day + timedelta(days=2), "100"),
            self.bar(day + timedelta(days=3), "101"),
            self.bar(day + timedelta(days=4), "1000"),
        ])
        self.assertEqual([entry["quality"] for entry in result], [
            HistoricalBar.Quality.DUPLICATE, HistoricalBar.Quality.DUPLICATE,
            HistoricalBar.Quality.INVALID, HistoricalBar.Quality.VALID,
            HistoricalBar.Quality.VALID, HistoricalBar.Quality.SPIKE,
        ])
        stored = store_bars(**self.identity, bars=[
            self.bar(day, "100"), self.bar(day, "101"),
            self.bar(day + timedelta(days=1), "0"),
        ])
        self.assertEqual(stored["inserted"], 1)
        self.assertEqual(HistoricalBar.objects.filter(quality="invalid").count(), 0)
        self.assertEqual(HistoricalBar.objects.filter(quality="duplicate").count(), 1)

    def test_database_rejects_invalid_ohlc_order_and_nonpositive_price(self):
        for values in (
            {"open": 10, "high": 5, "low": 1, "close": 8},
            {"open": 0, "high": 10, "low": 1, "close": 8},
        ):
            with self.subTest(values=values), self.assertRaises(IntegrityError), self.atomic_savepoint():
                    HistoricalBar.objects.create(
                        **self.identity, session_date=self.today, observed_at=timezone.now(),
                        volume=1, **values,
                    )

    from django.db import transaction

    def atomic_savepoint(self):
        return self.transaction.atomic()

    @property
    def transaction(self):
        from django.db import transaction
        return transaction

    def test_calendar_excludes_weekends_and_configured_exchange_holidays(self):
        sessions = expected_sessions(date(2025, 12, 24), date(2025, 12, 29), self.calendar)
        self.assertEqual(sessions, [date(2025, 12, 24), date(2025, 12, 26), date(2025, 12, 29)])

    def test_coverage_reports_warmup_and_calendar_gaps_for_all_windows(self):
        start = self.today - timedelta(days=430)
        sessions = expected_sessions(start, self.today, self.calendar)
        bars = [self.bar(day, 100 + index / 1000) for index, day in enumerate(sessions)]
        missing_day = sessions[-5]
        bars = [bar for bar in bars if bar.session_date != missing_day]
        store_bars(**self.identity, bars=bars)
        report = inspect_coverage(
            **{key: value for key, value in self.identity.items() if key != "adjustment"},
            end_date=self.today, calendar=self.calendar,
        )
        self.assertEqual(set(report["windows"]), {30, 90, 180, 365})
        self.assertIn(missing_day.isoformat(), report["windows"][30]["gap_dates"])
        self.assertFalse(report["windows"][30]["covered"])
        self.assertTrue(report["windows"][365]["warmup_ready"])

    def test_gap_marking_uses_calendar_and_does_not_fill_prices(self):
        start = date(2025, 12, 22)
        sessions = expected_sessions(start, date(2025, 12, 26), self.calendar)
        bars = [self.bar(day, 100) for day in sessions if day != date(2025, 12, 24)]
        validated = validate_bars(
            bars, expected_sessions=sessions, end_date=date(2025, 12, 26),
        )
        self.assertTrue(all(entry["quality"] == HistoricalBar.Quality.GAP for entry in validated))
        self.assertEqual(len(validated), 3)
        result = store_bars(**self.identity, bars=bars, calendar=self.calendar)
        self.assertEqual(result["inserted"], 3)
        self.assertFalse(HistoricalBar.objects.filter(session_date=date(2025, 12, 24)).exists())

    def test_replay_never_overwrites_first_provider_record(self):
        day = self.today - timedelta(days=2)
        first = self.bar(day, 100)
        corrected = self.bar(day, 200)
        store_bars(**self.identity, bars=[first])
        store_bars(**self.identity, bars=[corrected])
        row = HistoricalBar.objects.get(session_date=day)
        self.assertEqual(row.close, Decimal("100"))
