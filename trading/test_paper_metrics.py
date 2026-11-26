from datetime import UTC, datetime, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .backtesting import process_simulation
from .models import Account, CapitalSnapshot, Deposit, Instrument
from .paper_metrics import matching_paper_reference, paper_windows


@override_settings(PLATFORM_LAB_ENABLED=True)
class PaperMetricsTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("paper-operator")
        self.account = Account.objects.create(
            owner=user, name="Paper", mode="virtual", starting_cash=1000, cash=1000,
        )
        self.client.force_login(user)

    def mark(self, at, value):
        mark = CapitalSnapshot.objects.create(account=self.account, value=value)
        CapitalSnapshot.objects.filter(pk=mark.pk).update(captured_at=at)

    def test_deposit_is_excluded_from_window_profit(self):
        now = timezone.now()
        self.mark(now - timedelta(days=8), 1000)
        self.mark(now - timedelta(hours=1), 1120)
        deposit = self.account.deposit_set.create(reference="00000000-0000-0000-0000-000000000001",
                                                  amount=100, destination="cash", booked=True)
        Deposit.objects.filter(pk=deposit.pk).update(created=now - timedelta(days=2))
        week, month, quarter = paper_windows(self.account)
        self.assertEqual(week["gain"], Decimal(20))
        self.assertAlmostEqual(week["return_pct"], 2)
        self.assertFalse(month["available"])
        self.assertFalse(quarter["available"])

    def test_matched_simulation_uses_same_recorded_capital_and_reports_difference(self):
        start = datetime(2024, 2, 1, 20, tzinfo=UTC)
        end = start + timedelta(days=7)
        self.mark(start - timedelta(days=1), 1250)
        self.mark(end, 1310)
        self.assertEqual(matching_paper_reference(self.account, start.date(), end.date())["gain"], "60.00")
        histories = [[(start.date() + timedelta(days=i)).isoformat(), str(140 + i)] for i in range(-30, 8)]
        Instrument.objects.create(
            account=self.account, symbol="BTC/EUR", group="crypto", currency="EUR", history=histories,
        )
        response = self.client.post(reverse("simulations", args=[self.account.pk]), {
            "start": start.date(), "end": end.date(), "match_paper": "on",
            "fee_bps": 20, "spread_bps": 10, "slippage_bps": 5,
        })
        self.assertEqual(response.status_code, 302)
        process_simulation()
        job = self.account.simulation_set.get()
        self.assertEqual(job.capital, 1250)
        self.assertAlmostEqual(job.result["strategies"][0]["difference"], job.result["strategies"][0]["gain"] - 60)
