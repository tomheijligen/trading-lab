from datetime import timedelta
from decimal import Decimal

from django.core.paginator import Paginator
from django.db.models import Sum
from django.utils import timezone

from .models import Account, CapitalSnapshot, Trade, ZERO


CENT = Decimal("0.01")
CHART_DAYS = 30


def record_capital(account):
    """Record the balance using the latest stored position prices."""
    if account.mode != "virtual":
        return

    holdings = account.instrument_set.filter(units__gt=ZERO)
    value = account.cash + account.reserve + sum(
        (instrument.value for instrument in holdings),
        ZERO,
    )
    CapitalSnapshot.objects.create(account=account, value=value.quantize(CENT))


def owned_virtual_accounts(user, account_id=None):
    accounts = Account.objects.filter(owner=user, mode="virtual")
    if account_id is not None:
        accounts = accounts.filter(pk=account_id)
    return accounts


def order_history(request, account_id=None):
    """Return recorded virtual orders, newest first."""
    accounts = owned_virtual_accounts(request.user, account_id)
    trades = (
        Trade.objects.filter(instrument__account__in=accounts)
        .select_related("instrument__account")
        .order_by("-created", "-pk")
    )
    return Paginator(trades, 25).get_page(request.GET.get("trade_page"))


def chart_point(timestamp, value):
    return {"time": timestamp.isoformat(), "value": float(value)}


def capital_series(accounts, start):
    """Add the latest recorded values once every selected account has a value."""
    account_ids = list(accounts.values_list("pk", flat=True))
    if not account_ids:
        return []

    # Seed the chart with the last recorded value before the visible window.
    latest = dict(
        CapitalSnapshot.objects.filter(
            account_id__in=account_ids,
            captured_at__lt=start,
        )
        .order_by("account_id", "-captured_at", "-pk")
        .distinct("account_id")
        .values_list("account_id", "value")
    )

    points = []
    if len(latest) == len(account_ids):
        points.append(chart_point(start, sum(latest.values(), ZERO)))

    snapshots = (
        CapitalSnapshot.objects.filter(
            account_id__in=account_ids,
            captured_at__gte=start,
        )
        .order_by("captured_at", "pk")
        .values_list("account_id", "captured_at", "value")
    )

    for account_id, captured_at, value in snapshots.iterator():
        latest[account_id] = value
        if len(latest) == len(account_ids):
            points.append(chart_point(captured_at, sum(latest.values(), ZERO)))

    return points


def realized_series(accounts, start):
    """Use booked sale results to show cumulative profit after fees."""
    sales = Trade.objects.filter(
        instrument__account__in=accounts,
        side="Sell",
        profit__isnull=False,
    )
    total = sales.filter(created__lt=start).aggregate(total=Sum("profit"))["total"]
    recent = sales.filter(created__gte=start).order_by("created", "pk")

    if total is None:
        if not recent.exists():
            return []
        total = ZERO

    points = [chart_point(start, total)]
    for created, profit in recent.values_list("created", "profit").iterator():
        total += profit
        points.append(chart_point(created, total))

    return points


def performance_charts(user, account_id=None):
    accounts = owned_virtual_accounts(user, account_id)
    start = timezone.now() - timedelta(days=CHART_DAYS)
    return [
        {
            "title": "Capital history",
            "description": (
                "Cash, reserves and positions at their latest stored prices. "
                "Deposits can increase this line."
            ),
            "empty": "No capital snapshots yet. Run a virtual bot round to record the first value.",
            "color": "#48d6aa",
            "points": capital_series(accounts, start),
        },
        {
            "title": "Realized result",
            "description": "Cumulative result after fees from closed positions only.",
            "empty": "No recorded sales yet.",
            "color": "#aa90f7",
            "points": realized_series(accounts, start),
        },
    ]