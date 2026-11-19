from decimal import Decimal
from uuid import uuid4

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from .engine import signal_for
from .models import Account, Deposit


class AccessTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner")
        self.other = User.objects.create_user("other")
        self.account = Account.objects.create(
            owner=self.owner, name="Test account", mode="virtual",
            starting_cash=1000, cash=1000,
        )

    def test_another_user_cannot_read_or_trade(self):
        self.client.force_login(self.other)
        for name in ("account", "market", "reports"):
            url = reverse(name, args=[self.account.pk])
            self.assertEqual(self.client.get(url).status_code, 404)
        url = reverse("run", args=[self.account.pk])
        self.assertEqual(self.client.post(url, {"action": "trade"}).status_code, 404)

    def test_reserve_deposit_is_booked_exactly_once(self):
        self.client.force_login(self.owner)
        url = reverse("deposit", args=[self.account.pk])
        payload = {"reference": str(uuid4()), "amount": "30.00", "destination": "reserve"}
        self.assertEqual(self.client.post(url, payload).status_code, 302)
        self.assertEqual(self.client.post(url, payload).status_code, 302)
        self.account.refresh_from_db()
        self.assertEqual(self.account.reserve, Decimal("30.00"))
        self.assertEqual(self.account.cash, Decimal("1000.00"))
        self.assertEqual(Deposit.objects.count(), 1)

    def test_mutating_request_requires_csrf(self):
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.owner)
        url = reverse("run", args=[self.account.pk])
        self.assertEqual(browser.post(url, {"action": "trade"}).status_code, 403)

    def test_real_account_cannot_run_bot(self):
        self.account.mode = "real"
        self.account.save(update_fields=["mode"])
        self.client.force_login(self.owner)
        url = reverse("run", args=[self.account.pk])
        self.assertEqual(self.client.post(url, {"action": "trade"}).status_code, 404)

    def test_disabled_user_cannot_read_reports(self):
        self.client.force_login(self.owner)
        User.objects.filter(pk=self.owner.pk).update(is_active=False)
        self.assertEqual(self.client.get(reverse("reports", args=[self.account.pk])).status_code, 302)

    def test_strategy_signals_are_reproducible(self):
        prices = [Decimal(number) for number in range(1, 41)]
        for bot in ("trend", "cross", "momentum"):
            self.assertEqual(signal_for(bot, prices)[0], "buy")
            self.assertEqual(signal_for(bot, list(reversed(prices)))[0], "sell")