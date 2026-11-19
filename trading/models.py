from decimal import Decimal
from uuid import uuid4

from django.conf import settings
from django.db import models

ZERO = Decimal("0")
GROUPS = [
    ("stock", "Stocks"), ("etf", "ETFs"),
    ("option", "Options"), ("crypto", "Crypto"),
]
BOTS = [
    ("trend", "Trend - 20-day average"),
    ("cross", "Trend crossover - 10 versus 30 days"),
    ("momentum", "Momentum - 10-day return"),
    ("breakout", "Breakout - 20-day entry / 10-day exit"),
    ("reversion", "Mean reversion - 3% below average"),
    ("rsi", "RSI Reversal - 14 moves"),
    ("voltrend", "Volatility-Filtered Trend - 20 days"),
    ("pullback", "Trend Pullback - 5 and 20 days"),
    ("updays", "Up-Day Count - 7 of 10 moves"),
    ("hold", "Buy & Hold Benchmark"),
]


class PendingActivation(models.Model):
    # Deleting this record makes the activation link single-use.
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    token = models.UUIDField(default=uuid4, unique=True)
    created = models.DateTimeField(auto_now_add=True)


class Account(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=80)
    mode = models.CharField(max_length=8, choices=[
        ("virtual", "Virtual Trading"), ("real", "Real Account Register"),
    ])
    bot = models.CharField(max_length=12, choices=BOTS, default="trend")
    starting_cash = models.DecimalField(max_digits=18, decimal_places=2, default=ZERO)
    cash = models.DecimalField(max_digits=18, decimal_places=2, default=ZERO)
    reserve = models.DecimalField(max_digits=18, decimal_places=2, default=ZERO)
    stock = models.PositiveSmallIntegerField(default=33)
    etf = models.PositiveSmallIntegerField(default=33)
    option = models.PositiveSmallIntegerField(default=0)
    crypto = models.PositiveSmallIntegerField(default=33)
    automatic = models.BooleanField(default=False)
    auto_discovery = models.BooleanField(default=False)
    last_attempt = models.DateTimeField(null=True)


class Instrument(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE)
    symbol = models.CharField(max_length=35)
    exchange = models.CharField(max_length=35, blank=True)
    group = models.CharField(max_length=8, choices=GROUPS)
    # Keep discovered instruments and their trade history in the database.
    auto_discovered = models.BooleanField(default=False)
    stop = models.PositiveSmallIntegerField(default=0)
    trailing = models.PositiveSmallIntegerField(default=0)
    # Historical closes are stored as date/price pairs in their source currency.
    history = models.JSONField(default=list)
    currency = models.CharField(max_length=8, blank=True)
    source = models.CharField(max_length=20, blank=True)
    signal = models.CharField(max_length=8, blank=True)
    explanation = models.CharField(max_length=240, blank=True)
    # Keep the latest execution outcome separate from the market signal.
    execution_status = models.CharField(max_length=160, blank=True, default="")
    execution_checked_at = models.DateTimeField(null=True, blank=True)
    price = models.DecimalField(max_digits=24, decimal_places=8, null=True)
    quote_time = models.DateTimeField(null=True)
    units = models.DecimalField(max_digits=30, decimal_places=10, default=ZERO)
    cost = models.DecimalField(max_digits=18, decimal_places=2, default=ZERO)
    peak = models.DecimalField(max_digits=24, decimal_places=8, default=ZERO)
    entry_bar = models.DateField(null=True)
    exit_bar = models.DateField(null=True)
    stop_bar = models.DateField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["account", "symbol", "exchange"], name="unique_account_instrument"
        )]

    @property
    def value(self):
        return self.units * (self.price or ZERO)


class Deposit(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE)
    reference = models.UUIDField()
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    destination = models.CharField(max_length=8, choices=[
        ("cash", "Trading cash"), ("reserve", "Reserved cash"),
    ])
    booked = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["account", "reference"], name="unique_account_deposit"
        )]


class Trade(models.Model):
    instrument = models.ForeignKey(Instrument, on_delete=models.CASCADE)
    bot = models.CharField(max_length=12, choices=BOTS)
    side = models.CharField(max_length=4)
    price = models.DecimalField(max_digits=24, decimal_places=8)
    units = models.DecimalField(max_digits=30, decimal_places=10)
    fee = models.DecimalField(max_digits=18, decimal_places=2)
    profit = models.DecimalField(max_digits=18, decimal_places=2, null=True)
    reason = models.CharField(max_length=240)
    created = models.DateTimeField(auto_now_add=True, db_index=True)

class ReportSubscription(models.Model):
    class Period(models.TextChoices):
        DAY = "day", "Daily"
        WEEK = "week", "Weekly"
        MONTH = "month", "Monthly"
        QUARTER = "quarter", "Quarterly"
        HALF = "half", "Every six months"
        YEAR = "year", "Yearly"

    account = models.ForeignKey(
        Account, on_delete=models.CASCADE, related_name="report_subscriptions"
    )
    period = models.CharField(max_length=8, choices=Period.choices)

    # A new subscription starts counting from the time it was enabled.
    created = models.DateTimeField(auto_now_add=True)
    last_sent = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "period"], name="unique_account_report_period"
            )
        ]

class UserProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="trading_profile",
    )
    display_name = models.CharField(max_length=80, blank=True)

    # Store only the resized JPEG, never the original upload.
    avatar = models.BinaryField(null=True, blank=True, editable=False)

class CapitalSnapshot(models.Model):
    # Store the marked value of one virtual account at one point in time.
    account = models.ForeignKey(
        Account,
        on_delete=models.CASCADE,
        related_name="capital_snapshots",
    )
    captured_at = models.DateTimeField(auto_now_add=True)
    value = models.DecimalField(max_digits=18, decimal_places=2)

    class Meta:
        indexes = [
            models.Index(fields=["account", "captured_at"]),
        ]