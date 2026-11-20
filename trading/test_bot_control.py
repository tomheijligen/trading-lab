from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .bot_control import deployment_budget, switch_bot
from .engine import DataError, Snapshot, run_account
from .models import Account, BotSwitch, Instrument, Trade


@override_settings(PLATFORM_LAB_ENABLED=True)
class BotControlTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("operator")
        self.account = Account.objects.create(
            owner=self.user, name="Paper", mode="virtual", starting_cash=1000, cash=800,
        )
        self.position = Instrument.objects.create(
            account=self.account, group="crypto", symbol="BTC/EUR", units=2, cost=200,
            price=Decimal("110"), quote_time=timezone.now(),
        )

    def switch(self, treatment, bot="hold", revision=0):
        return switch_bot(
            account_id=self.account.pk, owner=self.user, revision=revision,
            new_bot=bot, treatment=treatment,
        )

    def test_sell_switch_is_atomic_and_cannot_be_replayed(self):
        change = self.switch("sell")
        self.assertEqual(change.value, Decimal("1020"))
        self.assertEqual(change.previous_bot, "trend")
        with self.assertRaises(DataError):
            self.switch("sell", bot="rsi")
        self.account.refresh_from_db()
        self.position.refresh_from_db()
        self.assertEqual(self.account.bot, "hold")
        self.assertEqual(self.account.cash, Decimal("1019.56"))
        self.assertEqual(self.position.units, 0)
        self.assertEqual(Trade.objects.get().bot, "trend")
        self.assertEqual(BotSwitch.objects.count(), 1)

    def test_failed_liquidation_changes_nothing(self):
        Instrument.objects.create(
            account=self.account, group="crypto", symbol="ETH/EUR", units=1,
        )
        with self.assertRaises(DataError):
            self.switch("sell")
        self.account.refresh_from_db()
        self.assertEqual(self.account.bot, "trend")
        self.assertFalse(Trade.objects.exists())
        self.assertFalse(BotSwitch.objects.exists())

    def test_freeze_then_transfer_management(self):
        self.switch("freeze")
        self.position.refresh_from_db()
        self.assertFalse(self.position.bot_managed)
        self.assertEqual(self.position.units, 2)
        self.switch("keep", bot="rsi", revision=1)
        self.position.refresh_from_db()
        self.assertTrue(self.position.bot_managed)

    def test_other_user_has_no_switch_access(self):
        self.client.force_login(get_user_model().objects.create_user("other"))
        response = self.client.post(
            reverse("bot_switch", args=[self.account.pk]),
            {"new_bot": "hold", "treatment": "sell", "revision": 0},
        )
        self.assertEqual(response.status_code, 404)

    def test_risk_headroom_includes_held_positions(self):
        self.account.refresh_from_db()
        self.account.max_position_pct = 20
        self.account.max_exposure_pct = 25
        self.position.refresh_from_db()
        self.assertEqual(deployment_budget(self.account, [self.position]), (204, 35))

    @patch("trading.engine.market_snapshot")
    def test_switch_invalidates_round_while_quote_was_in_flight(self, quote):
        now = timezone.now()

        def fetch(*args):
            self.switch("freeze")
            return Snapshot(
                [[(now.date() - timedelta(days=1)).isoformat(), "110"]],
                "EUR", now, Decimal("110"), True,
            )

        quote.side_effect = fetch
        self.assertIn("strategy changed", run_account(self.account.pk, execute=True))
        self.assertFalse(Trade.objects.exists())
