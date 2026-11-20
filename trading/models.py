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
    bot_revision = models.PositiveIntegerField(default=0)
    max_position_pct = models.PositiveSmallIntegerField(default=100)
    max_exposure_pct = models.PositiveSmallIntegerField(default=100)
    max_loss_pct = models.PositiveSmallIntegerField(default=0)


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
    quote_market_open = models.BooleanField(default=False)
    units = models.DecimalField(max_digits=30, decimal_places=10, default=ZERO)
    cost = models.DecimalField(max_digits=18, decimal_places=2, default=ZERO)
    peak = models.DecimalField(max_digits=24, decimal_places=8, default=ZERO)
    entry_bar = models.DateField(null=True)
    exit_bar = models.DateField(null=True)
    stop_bar = models.DateField(null=True)
    display_name = models.CharField(max_length=255, blank=True, default="")
    bot_managed = models.BooleanField(default=True)

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

class MarketCache(models.Model):
    key = models.CharField(max_length=64, unique=True)
    provider = models.CharField(max_length=24)
    payload = models.JSONField(null=True)
    fetched_at = models.DateTimeField(null=True)
    retry_at = models.DateTimeField(null=True)


class ProviderUsage(models.Model):
    provider = models.CharField(max_length=24, primary_key=True)
    minute = models.DateTimeField(null=True)
    day = models.DateField(null=True)
    minute_credits = models.PositiveIntegerField(default=0)
    day_credits = models.PositiveIntegerField(default=0)
    last_success = models.DateTimeField(null=True)
    last_failure = models.DateTimeField(null=True)
    blocked_until = models.DateTimeField(null=True)


class MarketBar(models.Model):
    provider = models.CharField(max_length=24)
    symbol = models.CharField(max_length=35)
    exchange = models.CharField(max_length=35)
    currency = models.CharField(max_length=8)
    day = models.DateField()
    close = models.DecimalField(max_digits=24, decimal_places=8)
    observed_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "symbol", "exchange", "currency", "day"],
                name="unique_provider_daily_bar",
            )
        ]


class BotSwitch(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE)
    previous_bot = models.CharField(max_length=12)
    new_bot = models.CharField(max_length=12)
    treatment = models.CharField(max_length=12)
    value = models.DecimalField(max_digits=18, decimal_places=2)
    created = models.DateTimeField(auto_now_add=True)


class Simulation(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE)
    start = models.DateField()
    end = models.DateField()
    capital = models.DecimalField(max_digits=18, decimal_places=2)
    fee_bps = models.PositiveSmallIntegerField(default=20)
    spread_bps = models.PositiveSmallIntegerField(default=10)
    slippage_bps = models.PositiveSmallIntegerField(default=5)
    inputs = models.JSONField(default=dict)
    paper_reference = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    status = models.CharField(max_length=8, default="queued", db_index=True)
    error = models.CharField(max_length=240, blank=True)
    created = models.DateTimeField(auto_now_add=True)


class MarketObservation(models.Model):
    group = models.CharField(max_length=8)
    symbol = models.CharField(max_length=35)
    requested_exchange = models.CharField(max_length=35)
    exchange = models.CharField(max_length=35)
    provider = models.CharField(max_length=24)
    currency = models.CharField(max_length=8)
    price_eur = models.DecimalField(max_digits=24, decimal_places=8)
    quote_time = models.DateTimeField()
    received_at = models.DateTimeField()
    market_open = models.BooleanField()
    history = models.JSONField(default=list)
    volume = models.DecimalField(max_digits=30, decimal_places=8, null=True)
    spread_bps = models.DecimalField(max_digits=12, decimal_places=4, null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["group", "symbol", "requested_exchange"],
                name="unique_market_observation",
            )
        ]

class HistoricalBar(models.Model):
    class Quality(models.TextChoices):
        VALID = "valid", "Valid"
        GAP = "gap", "Missing expected session"
        DUPLICATE = "duplicate", "Duplicate source record"
        INVALID = "invalid", "Invalid OHLCV values"
        SPIKE = "spike", "Extreme close-to-close move"
        WARMUP = "warmup", "Insufficient warm-up history"

    class Adjustment(models.TextChoices):
        RAW = "raw", "Raw"
        SPLIT = "split", "Split adjusted"
        DIVIDEND = "dividend", "Dividend adjusted"
        ALL = "all", "Fully adjusted"
        NONE = "none", "Provider adjustment unknown"

    provider = models.CharField(max_length=24)
    asset_class = models.CharField(max_length=8, choices=GROUPS)
    symbol = models.CharField(max_length=35)
    exchange = models.CharField(max_length=35)
    currency = models.CharField(max_length=8)
    session_date = models.DateField()
    observed_at = models.DateTimeField()
    open = models.DecimalField(max_digits=24, decimal_places=8)
    high = models.DecimalField(max_digits=24, decimal_places=8)
    low = models.DecimalField(max_digits=24, decimal_places=8)
    close = models.DecimalField(max_digits=24, decimal_places=8)
    volume = models.DecimalField(max_digits=30, decimal_places=8, null=True)
    adjustment = models.CharField(
        max_length=12, choices=Adjustment.choices, default=Adjustment.NONE,
    )
    quality = models.CharField(
        max_length=12, choices=Quality.choices, default=Quality.VALID, db_index=True,
    )
    quality_detail = models.CharField(max_length=240, blank=True)
    ingested_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "provider", "asset_class", "symbol", "exchange", "currency",
                    "session_date", "adjustment",
                ],
                name="unique_historical_market_bar",
            ),
            models.CheckConstraint(
                condition=models.Q(open__gt=0) & models.Q(high__gt=0)
                & models.Q(low__gt=0) & models.Q(close__gt=0),
                name="historical_bar_prices_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(high__gte=models.F("low"))
                & models.Q(high__gte=models.F("open"))
                & models.Q(high__gte=models.F("close"))
                & models.Q(low__lte=models.F("open"))
                & models.Q(low__lte=models.F("close")),
                name="historical_bar_ohlc_order",
            ),
            models.CheckConstraint(
                condition=models.Q(volume__isnull=True) | models.Q(volume__gte=0),
                name="historical_bar_volume_nonnegative",
            ),
        ]
        indexes = [
            models.Index(
                fields=["asset_class", "symbol", "exchange", "currency", "session_date"],
                name="historical_bar_instrument_day",
            ),
            models.Index(fields=["quality", "session_date"], name="historical_bar_quality_day"),
        ]
