from datetime import timedelta

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .engine import DataError, FEE, amount, record_trade
from .models import Account, BOTS, BotSwitch, ZERO

TREATMENTS = (
    ("keep", "Keep positions under the new strategy"),
    ("sell", "Sell existing positions virtually"),
    ("freeze", "Keep positions outside the new strategy"),
)


@transaction.atomic
def switch_bot(*, account_id, owner, revision, new_bot, treatment):
    account = Account.objects.select_for_update().get(
        pk=account_id, owner=owner, mode="virtual",
    )
    if account.bot_revision != revision:
        raise DataError("The account changed. Review the current settings and try again.")
    if new_bot == account.bot or new_bot not in dict(BOTS) or treatment not in dict(TREATMENTS):
        raise DataError("Choose a different strategy and a position treatment.")

    positions = list(account.instrument_set.filter(units__gt=ZERO).order_by("pk"))
    value = amount(account.cash + account.reserve + sum(item.value for item in positions))
    if treatment == "sell":
        now = timezone.now()
        if any(
            item.group == "option" or not item.price or not item.quote_time
            or not now - timedelta(minutes=30) <= item.quote_time <= now
            for item in positions
        ):
            raise DataError("Liquidation requires a fresh quote for every supported position.")
        if any(item.group != "crypto" and not item.quote_market_open for item in positions):
            raise DataError("Liquidation requires an open-session quote for each security.")
        if any(amount(item.units * item.price) <= ZERO for item in positions):
            raise DataError("A position is below the minimum sellable value.")
        for item in positions:
            gross = amount(item.units * item.price)
            fee = amount(gross * FEE)
            net = gross - fee
            record_trade(
                item, account, "Sell", item.price, item.units, fee,
                "Virtual liquidation at strategy switch.", net - item.cost,
            )
            account.cash += net
            item.units = item.cost = item.peak = ZERO
            item.entry_bar = item.stop_bar = None
            item.exit_bar = now.date()
            item.bot_managed = True
            item.save(update_fields=[
                "units", "cost", "peak", "entry_bar", "stop_bar", "exit_bar", "bot_managed",
            ])
    elif treatment == "freeze":
        account.instrument_set.filter(units__gt=ZERO).update(bot_managed=False)
    else:
        account.instrument_set.filter(bot_managed=False).update(bot_managed=True)

    switch = BotSwitch.objects.create(
        account=account, previous_bot=account.bot, new_bot=new_bot,
        treatment=treatment, value=value,
    )
    account.bot = new_bot
    account.bot_revision += 1
    account.save(update_fields=["bot", "bot_revision", "cash"])
    return switch


def deployment_budget(account, items):
    exposure = sum((item.value for item in items), ZERO)
    tradable_equity = account.cash + exposure
    headroom = tradable_equity * account.max_exposure_pct / 100 - exposure
    if account.max_loss_pct:
        deposits = account.deposit_set.filter(booked=True).aggregate(total=Sum("amount"))
        invested = account.starting_cash + (deposits["total"] or ZERO)
        if tradable_equity + account.reserve <= invested * (100 - account.max_loss_pct) / 100:
            headroom = ZERO
    return tradable_equity * account.max_position_pct / 100, headroom
