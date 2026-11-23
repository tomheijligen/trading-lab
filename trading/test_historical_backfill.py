import json
from datetime import UTC, date, datetime, time, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from .market_data import BINANCE
from .models import Account, HistoricalBar, Instrument, MarketBar, MarketCache, ProviderUsage, Trade

DAY = timedelta(days=1)
ZONE = ZoneInfo("America/New_York")
END = date(2025, 12, 29)
HOLIDAY = date(2025, 12, 25)
EARLY = date(2025, 12, 24)
PAIR = {
    "symbols": [{
        "symbol": "BTCEUR", "baseAsset": "BTC", "quoteAsset": "EUR",
        "isSpotTradingAllowed": True,
    }],
}


def date_range(start, end):
    return [start + index * DAY for index in range((end - start).days + 1)]


def milliseconds(day):
    return int(datetime.combine(day, time.min, UTC).timestamp()) * 1000


def klines(params, *, missing=()):
    start = datetime.fromtimestamp(params["startTime"] // 1000, UTC).date()
    end = datetime.fromtimestamp(params["endTime"] // 1000, UTC).date()
    return [
        [milliseconds(day), "100", "103", "98", "101", "12", milliseconds(day + DAY) - 1]
        for day in date_range(start, end) if day not in missing
    ]


class HistoricalBackfillTests(TestCase):
    def setUp(self):
        self.read = self.enterContext(patch("trading.market_data.read_json"))
        self.read.side_effect = AssertionError("Unexpected provider request")
        self.enterContext(patch.dict("os.environ", {
            "TWELVE_DATA_API_KEY": "fixture-twelve-secret",
            "APCA_API_KEY_ID": "fixture-alpaca-id",
            "APCA_API_SECRET_KEY": "fixture-alpaca-secret",
        }))
        directory = self.enterContext(TemporaryDirectory(dir=Path.cwd()))
        self.calendar = Path(directory, "venue.json")
        self.calendar_start = date(2024, 11, 1)
        self.closes = {
            day: datetime.combine(day, time(13 if day == EARLY else 16), ZONE)
            for day in date_range(self.calendar_start, END)
            if day.weekday() < 5 and day != HOLIDAY
        }
        self.write_calendar()

    def write_calendar(self, **changes):
        payload = {
            "exchange": "XNAS", "timezone": ZONE.key, "start": self.calendar_start.isoformat(),
            "end": END.isoformat(),
            "sessions": {day.isoformat(): close.isoformat() for day, close in self.closes.items()},
        }
        self.calendar.write_text(json.dumps({**payload, **changes}), encoding="utf-8")

    def args(self, provider="binance", *, apply=False, end=END):
        args = ["--provider", provider, "--end", end.isoformat()]
        if provider == "binance":
            args += ["--asset-class", "crypto", "--symbol", "BTC/EUR",
                     "--exchange", "BINANCE", "--currency", "EUR"]
        else:
            args += ["--asset-class", "stock", "--symbol", "ABC", "--exchange", "XNAS",
                     "--currency", "USD", "--calendar-file", str(self.calendar)]
        if apply:
            args.append("--apply")
        return args

    def run_command(self, *args, output=None):
        output = output if output is not None else StringIO()
        call_command("backfill_historical_data", *args, stdout=output)
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def binance(self, url, params, headers):
        if url.endswith("/exchangeInfo"):
            return PAIR
        self.assertTrue(url.endswith("/klines"))
        self.assertEqual(params["timeZone"], "0")
        self.assertEqual(params["interval"], "1d")
        self.assertEqual(params["startTime"] % 86_400_000, 0)
        self.assertEqual(params["endTime"] % 86_400_000, 86_399_999)
        return klines(params)

    def twelve(self, url, params, headers):
        self.assertTrue(url.endswith("/time_series"))
        self.assertEqual(params["interval"], "1day")
        self.assertEqual(params["mic_code"], "XNAS")
        self.assertNotIn("outputsize", params)
        start, stop = (date.fromisoformat(params[key]) for key in ("start_date", "end_date"))
        return {
            "meta": {"symbol": "ABC", "mic_code": "XNAS", "currency": "USD"},
            "values": [
                {"datetime": day.isoformat(), "open": "100", "high": "103",
                 "low": "98", "close": "101", "volume": "12"}
                for day in self.closes if start <= day <= stop
            ],
        }

    def test_dry_run_has_no_database_or_network_access(self):
        for days in (30, 90, 180, 365):
            with self.subTest(days=days), self.assertNumQueries(0):
                plan, = self.run_command(*self.args(), "--days", str(days))
                self.assertEqual(plan["mode"], "dry-run")
                self.assertEqual(plan["warmup_sessions"], 30)
                self.assertEqual(plan["expected_sessions"], days)
                self.assertEqual(plan["estimated_quota_units"], 20 + 2 * (plan["estimated_requests"] - 1))
        self.read.assert_not_called()

    def test_missing_mismatched_and_insufficient_calendars_stop_before_fetching(self):
        args = self.args("twelve", apply=True)
        with self.assertRaisesMessage(CommandError, "Calendar unreliable"):
            self.run_command(*args[:-3], "--apply")
        self.write_calendar(exchange="XNYS")
        with self.assertRaisesMessage(CommandError, "schedule MIC differs"):
            self.run_command(*args)
        self.write_calendar(start=END.isoformat(), sessions={END.isoformat(): self.closes[END].isoformat()})
        with self.assertRaisesMessage(CommandError, "outside the supplied schedule"):
            self.run_command(*args)
        self.read.assert_not_called()

    def test_binance_pages_cover_exact_bounds_and_resume_without_duplicate_storage(self):
        self.read.side_effect = self.binance
        args = self.args(apply=True) + ["--days", "365"]
        plan, result = self.run_command(*args)
        requests = [call.args[1] for call in self.read.call_args_list if call.args[0].endswith("/klines")]
        self.assertEqual(len(requests), 5)
        self.assertEqual(requests[0]["startTime"], milliseconds(date.fromisoformat(plan["fetch_start"])))
        self.assertEqual(requests[-1]["endTime"], milliseconds(END + DAY) - 1)
        self.assertEqual([page["endTime"] + 1 for page in requests[:-1]],
                         [page["startTime"] for page in requests[1:]])
        self.assertEqual(result["inserted"], 395)
        self.assertTrue(result["coverage"]["covered"])
        before = list(HistoricalBar.objects.order_by("pk").values())
        self.read.reset_mock()
        _, rerun = self.run_command(*args)
        self.assertEqual((rerun["inserted"], rerun["requests"]), (0, 0))
        self.read.assert_not_called()
        self.assertEqual(before, list(HistoricalBar.objects.order_by("pk").values()))

    def test_interrupted_run_commits_prior_chunks_and_resumes(self):
        def interrupted(url, params, headers):
            if url.endswith("/klines") and params["startTime"] > milliseconds(date(2025, 7, 1)):
                raise HTTPError(url, 403, "fixture-secret-in-provider-error", None, None)
            return self.binance(url, params, headers)

        self.read.side_effect = interrupted
        args = self.args(apply=True) + ["--days", "180"]
        output = StringIO()
        with self.assertRaisesMessage(CommandError, "rerun with the same arguments") as failure:
            self.run_command(*args, output=output)
        self.assertNotIn("fixture-secret", str(failure.exception) + output.getvalue())
        self.assertEqual(HistoricalBar.objects.count(), 90)
        first = HistoricalBar.objects.earliest("session_date")
        self.assertFalse(json.loads(output.getvalue().splitlines()[-1])["coverage"]["covered"])
        MarketCache.objects.update(retry_at=None)  # Advance beyond the failed request's lease.
        self.read.reset_mock()
        self.read.side_effect = self.binance
        _, resumed = self.run_command(*args)
        self.assertTrue(resumed["coverage"]["covered"])
        self.assertEqual(resumed["inserted"], 120)
        self.assertEqual(first.ingested_at, HistoricalBar.objects.get(pk=first.pk).ingested_at)
        self.assertEqual(self.read.call_count, 2)

    def test_partial_crypto_response_counts_duplicates_rejections_and_weekend_gaps(self):
        missing = date(2025, 12, 27)

        def partial(url, params, headers):
            if url.endswith("/exchangeInfo"):
                return PAIR
            rows = klines(params, missing={missing})
            return [*rows, rows[0], ["invalid"]]

        self.read.side_effect = partial
        args = self.args(apply=True)
        _, result = self.run_command(*args)
        self.assertEqual((result["duplicates"], result["rejected"]), (1, 1))
        self.assertEqual(result["coverage"]["gap_dates"], [missing.isoformat()])
        self.assertFalse(result["coverage"]["covered"])
        self.read.side_effect = self.binance
        _, later = self.run_command(*args)
        self.assertEqual(later["inserted"], 1)
        self.assertTrue(later["coverage"]["covered"])

    def test_twelve_filters_inclusive_boundary_and_respects_short_session(self):
        def with_extra_day(*args):
            payload = self.twelve(*args)
            payload["values"].append({**payload["values"][-1], "datetime": HOLIDAY.isoformat()})
            return payload

        self.read.side_effect = with_extra_day
        plan, result = self.run_command(
            *self.args("twelve", apply=True, end=EARLY), "--start", "2025-12-20", "--adjustment", "split",
        )
        params = self.read.call_args.args[1]
        self.assertEqual(params["adjust"], "splits")
        self.assertEqual(params["start_date"], plan["fetch_start"])
        self.assertEqual(params["end_date"], HOLIDAY.isoformat())
        self.assertEqual(result["coverage"]["expected_bars"], 3)
        self.assertEqual(result["rejected"], 0)
        self.assertTrue(result["coverage"]["covered"])
        early = HistoricalBar.objects.get(session_date=EARLY)
        self.assertEqual(early.observed_at, self.closes[EARLY])
        self.assertEqual((early.provider, early.adjustment, early.currency), ("Twelve Data", "split", "USD"))
        self.assertFalse(HistoricalBar.objects.filter(session_date__gt=EARLY).exists())

    def test_twelve_rejects_wrong_venue_or_currency(self):
        def wrong_venue(*args):
            payload = self.twelve(*args)
            payload["meta"]["mic_code"] = "XNYS"
            return payload

        self.read.side_effect = wrong_venue
        with self.assertRaisesMessage(CommandError, "does not match"):
            self.run_command(*self.args("twelve", apply=True))
        self.assertFalse(HistoricalBar.objects.exists())

    def test_twelve_crypto_uses_explicit_pair_and_venue(self):
        def crypto(url, params, headers):
            self.assertEqual((params["symbol"], params["exchange"], params["adjust"]),
                             ("BTC/EUR", "BINANCE", "none"))
            self.assertNotIn("mic_code", params)
            return {
                "meta": {"symbol": "BTC/EUR", "exchange": "Binance", "interval": "1day"},
                "values": [{"datetime": "2025-12-27", "open": "100", "high": "103",
                            "low": "98", "close": "101"}],
            }

        self.read.side_effect = crypto
        args = self.args(apply=True)
        args[1] = "twelve"
        _, result = self.run_command(*args)
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(HistoricalBar.objects.get().currency, "EUR")

    def test_alpaca_follows_tokens_and_preserves_provider_and_adjustment_isolation(self):
        self.read.side_effect = self.twelve
        self.run_command(*self.args("twelve", apply=True), "--adjustment", "split")
        self.read.reset_mock()

        def alpaca(url, params, headers):
            self.assertTrue(url.endswith("/v2/stocks/bars"))
            self.assertEqual((params["feed"], params["currency"], params["asof"]), ("iex", "USD", "-"))
            start, end = (datetime.fromisoformat(params[key]).date() for key in ("start", "end"))
            session_days = [day for day in self.closes if start <= day <= end]
            continuing = "page_token" in params
            if continuing:
                self.assertEqual(params["page_token"], "next-fixture")
            return {
                "bars": {"ABC": [
                    {"t": datetime.combine(day, time.min, ZONE).astimezone(UTC).isoformat(),
                     "o": 100, "h": 103, "l": 98, "c": 101, "v": 12}
                    for day in (session_days[15:] if continuing else session_days[:15])
                ]},
                "next_page_token": None if continuing else "next-fixture",
            }

        self.read.side_effect = alpaca
        for adjustment in ("split", "raw"):
            _, result = self.run_command(*self.args("alpaca", apply=True), "--adjustment", adjustment)
            self.assertTrue(result["coverage"]["covered"])
            self.assertEqual(result["requests"], 2)
        self.assertEqual(set(HistoricalBar.objects.values_list("provider", "adjustment")), {
            ("Twelve Data", "split"), ("Alpaca IEX", "split"), ("Alpaca IEX", "raw"),
        })

    def test_alpaca_stops_repeating_page_token_without_committing_chunk(self):
        self.read.side_effect = None
        self.read.return_value = {"bars": {}, "next_page_token": "private-token"}
        with self.assertRaisesMessage(CommandError, "repeated historical page token") as failure:
            self.run_command(*self.args("alpaca", apply=True))
        self.assertNotIn("private-token", str(failure.exception))
        self.assertEqual(self.read.call_count, 2)
        self.assertFalse(HistoricalBar.objects.exists())

    def test_missing_credentials_stop_without_external_request(self):
        with patch.dict("os.environ", {"TWELVE_DATA_API_KEY": "", "APCA_API_SECRET_KEY": ""}):
            for provider in ("twelve", "alpaca"):
                with self.subTest(provider=provider), self.assertRaises(CommandError):
                    self.run_command(*self.args(provider, apply=True))
        self.read.assert_not_called()
        self.assertFalse(HistoricalBar.objects.exists())

    def test_binance_pair_mismatch_stops_before_candles(self):
        self.read.side_effect = None
        self.read.return_value = {"symbols": [{**PAIR["symbols"][0], "baseAsset": "OTHER"}]}
        with self.assertRaisesMessage(CommandError, "does not match the requested Spot pair"):
            self.run_command(*self.args(apply=True))
        self.assertEqual(self.read.call_count, 1)
        self.assertFalse(HistoricalBar.objects.exists())

    def test_shared_quota_and_remote_throttle_never_retry(self):
        with patch.object(BINANCE, "minute_limit", 20), patch("trading.historical_backfill.sleep") as wait:
            self.read.side_effect = self.binance
            with self.assertRaisesMessage(CommandError, "request budget"):
                self.run_command(*self.args(apply=True))
            self.assertEqual(self.read.call_count, 1)
            wait.assert_not_called()
        self.read.reset_mock()
        self.read.side_effect = None
        self.read.return_value = {"status": "error", "code": "429", "message": "private"}
        with patch("trading.historical_backfill.sleep") as wait:
            with self.assertRaises(CommandError):
                self.run_command(*self.args("twelve", apply=True))
            wait.assert_not_called()
        self.assertEqual(self.read.call_count, 1)
        self.assertIsNotNone(ProviderUsage.objects.get(provider="Twelve Data").blocked_until)

    def test_temporary_errors_retry_once_after_lease(self):
        self.read.side_effect = [TimeoutError(), TimeoutError()]
        with patch("trading.historical_backfill.sleep", side_effect=lambda _: MarketCache.objects.update(
            retry_at=None,
        )) as wait:
            with self.assertRaises(CommandError):
                self.run_command(*self.args("twelve", apply=True))
        wait.assert_called_once_with(30)
        self.assertEqual(self.read.call_count, 2)
        self.assertFalse(HistoricalBar.objects.exists())

    def test_temporary_error_can_recover_within_the_run(self):
        self.read.side_effect = TimeoutError()

        def expire_lease(_):
            MarketCache.objects.update(retry_at=None)
            self.read.side_effect = self.twelve

        with patch("trading.historical_backfill.sleep", side_effect=expire_lease):
            _, result = self.run_command(*self.args("twelve", apply=True))
        self.assertEqual((result["requests"], result["retries"]), (2, 1))
        self.assertTrue(result["coverage"]["covered"])

    def test_apply_does_not_touch_existing_trading_data_or_legacy_market_bars(self):
        owner = get_user_model().objects.create_user(username="fixture")
        account = Account.objects.create(owner=owner, mode="virtual", name="Fixture", cash=500, bot="trend")
        instrument = Instrument.objects.create(account=account, symbol="BTC/EUR", group="crypto", units=2)
        Trade.objects.create(instrument=instrument, side="Buy", bot="trend", price=100, units=2, fee=1)
        models = (Account, Instrument, Trade, MarketBar)
        before = {model: list(model.objects.values()) for model in models}
        self.read.side_effect = self.binance
        _, result = self.run_command(*self.args(apply=True))
        self.assertTrue(result["coverage"]["covered"])
        self.assertEqual(before, {model: list(model.objects.values()) for model in models})
