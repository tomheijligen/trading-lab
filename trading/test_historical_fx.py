import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch
from urllib.error import HTTPError

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase

from .historical_fx import (
    FX_SERIES, FX_ZONE, FXInput, HistoricalFXUnavailable, convert_historical_fx,
    fx_available_at, historical_fx_rate, inspect_fx_coverage, store_fx_rates,
)
from .models import Account, HistoricalFXRate, Instrument, MarketCache, ProviderUsage, Trade

FRIDAY = date(2025, 10, 3)
MONDAY = date(2025, 10, 6)
TUESDAY = date(2025, 10, 7)
WEDNESDAY = date(2025, 10, 8)


class FXStorageTests(TestCase):
    def test_repeated_rates_are_idempotent_and_conflicts_preserve_original(self):
        fixture = FXInput(FRIDAY, "1.25")
        self.assertEqual(store_fx_rates(iter([fixture, fixture])), {
            "inserted": 1, "duplicates": 1, "rejected": 0, "conflicts": 0,
        })
        first = HistoricalFXRate.objects.get()
        self.assertEqual(store_fx_rates([fixture, FXInput(MONDAY, "1.1"), FXInput(MONDAY, "1.2")]), {
            "inserted": 0, "duplicates": 1, "rejected": 0, "conflicts": 2,
        })
        self.assertEqual(store_fx_rates([FXInput(FRIDAY, "1.26")])["conflicts"], 1)
        current = HistoricalFXRate.objects.get()
        self.assertEqual((current.rate, current.ingested_at), (first.rate, first.ingested_at))

    def test_validation_rejects_bad_rates_dates_and_unavailable_sessions(self):
        now = fx_available_at(MONDAY) - timedelta(minutes=1)
        with patch("trading.historical_fx.timezone.now", return_value=now):
            result = store_fx_rates([
                *(FXInput(FRIDAY, rate) for rate in ("NaN", "Infinity", 0, -1, "1e-13", None)),
                FXInput(MONDAY, 1), FXInput(FRIDAY + timedelta(days=1), 1),
                FXInput(FRIDAY, 1, "unknown"), None,
            ])
        self.assertEqual(result["rejected"], 10)
        self.assertFalse(HistoricalFXRate.objects.exists())

    def test_coverage_separates_suspect_missing_and_weekends(self):
        store_fx_rates([FXInput(FRIDAY, 1), FXInput(MONDAY, 1, HistoricalFXRate.Quality.SUSPECT)])
        self.assertEqual(inspect_fx_coverage(FRIDAY, TUESDAY), {
            "expected_sessions": 3, "valid_rates": 1, "covered": False,
            "missing_dates": [TUESDAY.isoformat()], "suspect_dates": [MONDAY.isoformat()],
        })
        with self.assertRaisesMessage(HistoricalFXUnavailable, MONDAY.isoformat()):
            historical_fx_rate("USD", "EUR", as_of=fx_available_at(MONDAY))

    def test_database_enforces_positive_canonical_unique_rates(self):
        expected = {**FX_SERIES, "session_date": FRIDAY, "rate": 1, "available_at": fx_available_at(FRIDAY)}
        HistoricalFXRate.objects.create(**expected)
        for invalid in (
            {}, {"session_date": MONDAY, "rate": 0},
            {"base_currency": "USD", "quote_currency": "EUR"},
            {"session_date": MONDAY, "quality": "unknown"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(IntegrityError), transaction.atomic():
                HistoricalFXRate.objects.create(**{**expected, **invalid})


class FXConversionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        store_fx_rates([FXInput(FRIDAY, "1.25"), FXInput(MONDAY, "2")])

    def test_direct_and_inverse_conversion_use_one_series(self):
        as_of = fx_available_at(FRIDAY)
        self.assertEqual(convert_historical_fx("125", "USD", "EUR", as_of=as_of), Decimal("100"))
        self.assertEqual(convert_historical_fx("100", "EUR", "USD", as_of=as_of), Decimal("125"))
        with self.assertNumQueries(0):
            self.assertEqual(convert_historical_fx("100", "EUR", "EUR", as_of=as_of), Decimal("100"))
        self.assertEqual(HistoricalFXRate.objects.filter(base_currency="USD").count(), 0)

    def test_weekend_and_dst_use_later_close_only_on_its_cutoff(self):
        monday_cutoff = fx_available_at(MONDAY).astimezone(UTC)
        self.assertEqual(fx_available_at(FRIDAY).astimezone(UTC).hour, 15)
        self.assertEqual(monday_cutoff.hour, 14)
        for when in (datetime(2025, 10, 5, 12, tzinfo=UTC), monday_cutoff - timedelta(microseconds=1)):
            self.assertEqual(historical_fx_rate("EUR", "USD", as_of=when), Decimal("1.25"))
        for when in (monday_cutoff, monday_cutoff.astimezone(FX_ZONE)):
            self.assertEqual(historical_fx_rate("EUR", "USD", as_of=when), Decimal("2"))

    def test_missing_weekday_blocks_carry_forward_and_future_row(self):
        store_fx_rates([FXInput(WEDNESDAY, "3")])
        with self.assertRaisesMessage(HistoricalFXUnavailable, TUESDAY.isoformat()):
            historical_fx_rate("EUR", "USD", as_of=fx_available_at(TUESDAY))

    def test_rejects_stale_naive_unknown_and_nonfinite_inputs(self):
        sunday = datetime(2025, 10, 5, 10, tzinfo=UTC)
        with self.assertRaisesMessage(HistoricalFXUnavailable, "maximum age"):
            historical_fx_rate("EUR", "USD", as_of=sunday, max_age=timedelta(hours=24))
        for args in (("USD", "EUR", sunday.replace(tzinfo=None)), ("GBP", "EUR", sunday)):
            with self.assertRaises(ValueError):
                historical_fx_rate(args[0], args[1], as_of=args[2])
        with self.assertRaises(ValueError):
            convert_historical_fx("NaN", "USD", "EUR", as_of=sunday)
        with self.assertRaises(ValueError):
            historical_fx_rate("EUR", "USD", as_of=sunday, max_age=timedelta(hours=97))


class FXBackfillTests(TestCase):
    def setUp(self):
        self.read = self.enterContext(patch("trading.market_data.read_json"))
        self.read.side_effect = AssertionError("Unexpected Twelve Data request")
        self.enterContext(patch.dict("os.environ", {"TWELVE_DATA_API_KEY": "synthetic-secret"}))

    def run_backfill(self, *args, output=None):
        output = output if output is not None else StringIO()
        call_command("backfill_historical_fx", "--start", FRIDAY.isoformat(),
                     "--end", MONDAY.isoformat(), *args, stdout=output)
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def payload(self, *days):
        return {
            "meta": {"symbol": "EUR/USD", "interval": "1day"},
            "values": [{"datetime": day.isoformat(), "close": "1.25"} for day in days],
        }

    def test_default_dry_run_is_offline_for_either_pair(self):
        for pair in ("EUR/USD", "USD/EUR"):
            with self.assertNumQueries(0):
                plan, = self.run_backfill("--pair", pair)
            self.assertEqual((plan["mode"], plan["expected_sessions"]), ("dry-run", 2))
            self.assertEqual(plan["series"], FX_SERIES)
        self.read.assert_not_called()

    def test_backfill_resumes_gaps_and_both_directions_share_storage(self):
        self.read.side_effect = [
            self.payload(FRIDAY, FRIDAY, FRIDAY + timedelta(days=1), TUESDAY),
            self.payload(MONDAY),
        ]
        _, first = self.run_backfill("--apply", "--pair", "USD/EUR")
        self.assertEqual((first["inserted"], first["duplicates"], first["rejected"]), (1, 1, 1))
        self.assertEqual(first["coverage"]["missing_dates"], [MONDAY.isoformat()])
        url, params, _ = self.read.call_args.args
        self.assertTrue(url.endswith("/time_series"))
        self.assertEqual(params, {
            "symbol": "EUR/USD", "interval": "1day", "adjust": "none", "order": "asc",
            "start_date": FRIDAY.isoformat(), "end_date": TUESDAY.isoformat(),
        })
        _, second = self.run_backfill("--apply")
        self.assertEqual((second["inserted"], second["coverage"]["covered"]), (1, True))
        self.assertEqual(self.read.call_args.args[1]["start_date"], MONDAY.isoformat())
        _, repeated = self.run_backfill("--apply", "--pair", "USD/EUR")
        self.assertEqual((repeated["requests"], repeated["inserted"]), (0, 0))
        self.assertEqual(self.read.call_count, 2)

    def test_provider_rate_limit_stops_without_retry_and_scrubs_error(self):
        self.read.side_effect = HTTPError("https://example.invalid", 429, "synthetic-secret", {}, None)
        with patch("trading.historical_fx_backfill.sleep") as wait:
            output = StringIO()
            with self.assertRaises(CommandError) as error:
                self.run_backfill("--apply", output=output)
            wait.assert_not_called()
        self.assertNotIn("synthetic-secret", str(error.exception) + output.getvalue())
        self.assertEqual(self.read.call_count, 1)
        self.assertIsNotNone(ProviderUsage.objects.get().blocked_until)
        self.assertFalse(HistoricalFXRate.objects.exists())

    def test_transient_error_retries_once_and_missing_credentials_make_no_request(self):
        self.read.side_effect = [TimeoutError(), self.payload(FRIDAY, MONDAY)]
        with patch("trading.historical_fx_backfill.sleep", side_effect=lambda _: MarketCache.objects.update(
            retry_at=None,
        )) as wait:
            _, result = self.run_backfill("--apply")
        wait.assert_called_once_with(30)
        self.assertEqual((result["requests"], result["retries"], result["inserted"]), (2, 1, 2))
        HistoricalFXRate.objects.all().delete()
        self.read.reset_mock()
        with patch.dict("os.environ", {"TWELVE_DATA_API_KEY": ""}), self.assertRaises(CommandError):
            self.run_backfill("--apply")
        self.read.assert_not_called()

    def test_wrong_pair_and_unavailable_session_stop_safely(self):
        with patch("trading.historical_fx_backfill.timezone.now",
                   return_value=fx_available_at(MONDAY) - timedelta(minutes=1)):
            with self.assertRaisesMessage(CommandError, "availability cutoff"):
                self.run_backfill("--apply")
        self.read.assert_not_called()
        self.read.side_effect = None
        self.read.return_value = {"meta": {"symbol": "USD/EUR"}, "values": []}
        with self.assertRaisesMessage(CommandError, "canonical daily FX series"):
            self.run_backfill("--apply")
        self.assertFalse(HistoricalFXRate.objects.exists())

    def test_fx_backfill_leaves_existing_trading_state_untouched(self):
        owner = get_user_model().objects.create_user(username="fixture")
        account = Account.objects.create(owner=owner, mode="virtual", name="Fixture", cash=500, bot="trend")
        instrument = Instrument.objects.create(account=account, symbol="ABC", group="stock", units=2)
        Trade.objects.create(instrument=instrument, side="Buy", bot="trend", price=100, units=2, fee=1)
        models = (Account, Instrument, Trade)
        original = {model: list(model.objects.values()) for model in models}
        self.read.side_effect = None
        self.read.return_value = self.payload(FRIDAY, MONDAY)
        self.run_backfill("--apply")
        self.assertEqual(original, {model: list(model.objects.values()) for model in models})
