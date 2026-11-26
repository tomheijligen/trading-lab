from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from .backtesting import process_simulation, replay
from .models import Account, Simulation, Trade


@override_settings(PLATFORM_LAB_ENABLED=True)
class BacktestingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("researcher")
        self.account = Account.objects.create(
            owner=self.user, name="Research", mode="virtual", starting_cash=5000, cash=5000,
        )
        first = date(2024, 1, 1)
        self.prices = {first + timedelta(days=i): Decimal(100 + i) for i in range(130)}
        self.start = first + timedelta(days=30)
        self.end = first + timedelta(days=129)

    def job(self):
        return Simulation.objects.create(
            account=self.account, start=self.start, end=self.end, capital=5000,
            inputs={"BTC/EUR": {day.isoformat(): str(close) for day, close in self.prices.items()}},
        )

    def test_signal_does_not_read_execution_close(self):
        histories = {"BTC/EUR": self.prices}
        with patch("trading.backtesting.signal_for", return_value=("buy", "")) as signal:
            result = replay(
                "hold", histories, self.start, self.start, Decimal(5000), Decimal(0), Decimal(0),
            )
        self.assertEqual(signal.call_args.args[1][-1], self.prices[self.start - timedelta(days=1)])
        self.assertEqual(result["orders"], 1)
        self.assertEqual(result["gain"], 0)

    def test_costs_reduce_gain_for_same_strategy_and_capital(self):
        inputs = ("hold", {"BTC/EUR": self.prices}, self.start, self.end, Decimal(5000))
        gross = replay(*inputs, Decimal(0), Decimal(0))
        net = replay(*inputs, Decimal(".002"), Decimal(".001"))
        self.assertGreater(gross["gain"], net["gain"])
        self.assertGreater(net["fees"], 0)

    def test_worker_result_is_persistent_and_does_not_trade(self):
        job = self.job()
        call_command("run_simulations")
        job.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(job.status, "done")
        self.assertEqual(len(job.result["strategies"]), 10)
        self.assertEqual(len(job.result["validation"]), 2)
        self.assertEqual(self.account.cash, 5000)
        self.assertFalse(Trade.objects.exists())
        self.assertFalse(process_simulation())

    def test_worker_interruption_leaves_job_available(self):
        job = self.job()
        with patch("trading.backtesting.calculate", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                process_simulation()
        job.refresh_from_db()
        self.assertEqual(job.status, "queued")
        self.assertTrue(process_simulation())

    def test_exports_are_owner_scoped(self):
        job = self.job()
        process_simulation()
        url = reverse("simulation_export", args=[self.account.pk, job.pk])
        self.client.force_login(get_user_model().objects.create_user("outsider"))
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(url).json()["type"], "historical_simulation")
        self.assertContains(
            self.client.get(reverse("simulations", args=[self.account.pk])),
            "Walk-forward tests",
        )
