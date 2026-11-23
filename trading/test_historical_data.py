from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, UTC
from decimal import Decimal
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.db import IntegrityError, connection, connections, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from .historical_data import (
    BarInput, DailyCalendar, WeekdayCalendar, expected_sessions, inspect_coverage,
    store_bars, validate_bars,
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
            HistoricalBar.Quality.INVALID, HistoricalBar.Quality.INVALID,
            HistoricalBar.Quality.INVALID, HistoricalBar.Quality.VALID,
            HistoricalBar.Quality.VALID, HistoricalBar.Quality.SPIKE,
        ])
        stored = store_bars(**self.identity, bars=[
            self.bar(day, "100"), self.bar(day, "101"),
            self.bar(day + timedelta(days=1), "0"),
        ])
        self.assertEqual(stored["inserted"], 0)
        self.assertEqual(stored["rejected"], 3)
        self.assertEqual(stored["duplicates"], 0)
        self.assertFalse(HistoricalBar.objects.exists())

    def test_database_rejects_invalid_ohlc_order_and_nonpositive_price(self):
        for values in (
            {"open": 10, "high": 5, "low": 1, "close": 8},
            {"open": 0, "high": 10, "low": 1, "close": 8},
        ):
            with self.subTest(values=values), self.assertRaises(IntegrityError), transaction.atomic():
                HistoricalBar.objects.create(
                    **self.identity, session_date=self.today, observed_at=timezone.now(),
                    volume=1, **values,
                )

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
            **self.identity, end_date=self.today, calendar=self.calendar,
        )
        self.assertEqual(set(report["windows"]), {30, 90, 180, 365})
        self.assertIn(missing_day.isoformat(), report["windows"][30]["gap_dates"])
        self.assertFalse(report["windows"][30]["covered"])
        self.assertTrue(report["windows"][365]["warmup_ready"])

    def test_filling_gap_restores_coverage_without_changing_neighbor_candles(self):
        missing = date(2025, 12, 24)
        bars = [self.bar(day, 100) for day in self.sessions() if day != missing]
        result = store_bars(**self.identity, bars=bars, calendar=self.calendar)
        self.assertEqual(result["quality"], {"valid": len(bars)})
        original = set(HistoricalBar.objects.values_list("pk", "close", "ingested_at"))
        self.assertEqual(self.coverage()["gap_dates"], [missing.isoformat()])
        self.assertFalse(self.coverage()["covered"])
        store_bars(**self.identity, bars=[self.bar(missing, 100)], calendar=self.calendar)
        self.assertTrue(self.coverage()["covered"])
        self.assertEqual(
            original,
            set(HistoricalBar.objects.exclude(session_date=missing).values_list(
                "pk", "close", "ingested_at",
            )),
        )

    def test_replay_never_overwrites_first_provider_record(self):
        day = self.today - timedelta(days=2)
        first = self.bar(day, 100)
        corrected = self.bar(day, 200)
        store_bars(**self.identity, bars=[first])
        self.assertEqual(store_bars(**self.identity, bars=[corrected])["duplicates"], 1)
        row = HistoricalBar.objects.get(session_date=day)
        self.assertEqual(row.close, Decimal("100"))

    def sessions(self, calendar=None):
        return expected_sessions(
            self.today - timedelta(days=100), self.today, calendar or self.calendar,
        )

    def coverage(self, **kwargs):
        options = {**self.identity, "calendar": self.calendar, "end_date": self.today, **kwargs}
        return inspect_coverage(**options)["windows"][30]

    def test_calendar_required_even_with_complete_history(self):
        store_bars(**self.identity, bars=[self.bar(day, 100) for day in self.sessions()])
        self.assertTrue(self.coverage()["covered"])
        window = self.coverage(calendar=None)
        self.assertEqual(window["calendar_status"], "unavailable")
        self.assertIsNone(window["expected_bars"])
        self.assertFalse(window["covered"])
        self.assertFalse(window["warmup_ready"])

    def test_only_immediately_preceding_expected_sessions_count_for_warmup(self):
        missing = date(2025, 11, 28)
        store_bars(**self.identity, bars=[
            self.bar(day, 100) for day in self.sessions() if day != missing
        ])
        window = self.coverage()
        self.assertEqual(window["warmup_bars"], 29)
        self.assertEqual(window["warmup_unusable_dates"], [missing.isoformat()])
        self.assertEqual(window["gap_dates"], [])
        self.assertFalse(window["warmup_ready"])
        self.assertFalse(window["covered"])

    def test_only_valid_session_bars_count_and_period_edges_are_required(self):
        start, rejected = date(2025, 12, 1), date(2025, 12, 24)
        store_bars(**self.identity, bars=[
            self.bar(day, 100) for day in self.sessions() if day not in {start, self.today}
        ] + [self.bar(date(2025, 12, 25), 100)])
        HistoricalBar.objects.filter(session_date=rejected).update(quality=HistoricalBar.Quality.SPIKE)
        window = self.coverage()
        self.assertEqual(window["gap_dates"], [start.isoformat(), self.today.isoformat()])
        self.assertEqual(window["rejected_dates"], [rejected.isoformat()])
        self.assertEqual(window["available_bars"], window["expected_bars"] - 3)
        self.assertFalse(window["covered"])

    def test_series_dimensions_do_not_supply_missing_bars(self):
        store_bars(**self.identity, bars=[
            self.bar(day, 100) for day in self.sessions() if day != self.today
        ])
        for field, value in {
            "provider": "Other", "asset_class": "etf", "symbol": "DEF",
            "exchange": "NYSE", "currency": "EUR", "adjustment": "raw",
        }.items():
            with self.subTest(field=field):
                alternate = {**self.identity, field: value}
                store_bars(**alternate, bars=[self.bar(self.today, 100)])
                self.assertEqual(self.coverage(**{field: value})["available_bars"], 1)
                self.assertFalse(self.coverage(**{field: value})["covered"])
        self.assertEqual(self.coverage()["gap_dates"], [self.today.isoformat()])

    def test_crypto_requires_weekends_and_holidays(self):
        calendar = DailyCalendar()
        crypto = {"asset_class": "crypto", "symbol": "BTC/EUR", "exchange": "BINANCE", "currency": "EUR"}
        missing = date(2025, 12, 27)
        store_bars(**{**self.identity, **crypto}, calendar=calendar, bars=[
            self.bar(day, 100) for day in self.sessions(calendar) if day != missing
        ])
        window = self.coverage(calendar=calendar, **crypto)
        self.assertEqual(window["expected_bars"], 31)
        self.assertEqual(window["gap_dates"], [missing.isoformat()])
        self.assertFalse(window["covered"])

    def test_lists_and_generators_count_every_input_once(self):
        valid = self.bar(date(2025, 12, 24), 100)
        bars = [
            valid, valid, replace(valid, session_date="invalid"),
            replace(valid, observed_at=valid.observed_at.replace(tzinfo=None)),
            self.bar(date(2025, 12, 25), 100),
            self.bar(date(2025, 12, 26), 0),
        ]
        result = store_bars(**self.identity, bars=bars, calendar=self.calendar)
        generated = store_bars(
            **{**self.identity, "symbol": "GEN"}, bars=(bar for bar in bars), calendar=self.calendar,
        )
        self.assertEqual(result, generated)
        self.assertEqual(result, {
            "inserted": 1, "duplicates": 1, "rejected": 4,
            "quality": {"valid": 1, "duplicate": 1, "invalid": 4},
        })

    def test_unrelated_database_constraint_failure_rolls_back_batch(self):
        bars = [self.bar(date(2025, 12, 24), 100), self.bar(date(2025, 12, 26), 100)]
        validated = validate_bars(bars)
        validated[1]["low"] = Decimal("0")
        with patch("trading.historical_data.validate_bars", return_value=validated):
            with self.assertRaises(IntegrityError):
                store_bars(**self.identity, bars=bars)
        self.assertFalse(HistoricalBar.objects.exists())


@skipUnless(connection.vendor == "postgresql", "Requires concurrent PostgreSQL writers")
class HistoricalDataConcurrencyTests(TransactionTestCase):
    def test_concurrent_inserts_store_one_bar_and_count_one_duplicate(self):
        barrier = Barrier(2, timeout=10)
        day = date(2025, 12, 24)
        bar = BarInput(day, datetime(2025, 12, 24, 22, tzinfo=UTC), 100, 101, 99, 100)

        def insert():
            try:
                barrier.wait()
                return store_bars(
                    provider="Fixture", asset_class="stock", symbol="ABC", exchange="NASDAQ",
                    currency="USD", adjustment="raw", bars=[bar],
                )
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(insert) for _ in range(2)]
            results = [future.result(timeout=15) for future in futures]
        self.assertEqual(sorted((r["inserted"], r["duplicates"]) for r in results), [(0, 1), (1, 0)])
        self.assertEqual(HistoricalBar.objects.count(), 1)
