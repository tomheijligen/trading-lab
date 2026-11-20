from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .engine import FEE, amount, signal_for
from .models import Account, BOTS, Deposit, Trade, ZERO
from .performance import order_history

MIN_HISTORY = 80
WARMUP = 30
MIN_VALIDATION_EXITS = 3
MIN_IMPROVEMENT = Decimal("0.50")
ONE = Decimal("1")
HUNDRED = Decimal("100")
BOT_LABELS = dict(BOTS)


def account_results(user):
    """Build all-time results from the signed-in user's own virtual accounts."""
    accounts = list(
        Account.objects.filter(owner=user, mode="virtual")
        .prefetch_related("instrument_set")
        .order_by("name", "pk")
    )

    deposits = dict(
        Deposit.objects.filter(account__in=accounts, booked=True)
        .values("account_id")
        .annotate(total=Sum("amount"))
        .values_list("account_id", "total")
    )

    exits = (
        Trade.objects.filter(instrument__account__in=accounts, side="Sell")
        .values("instrument__account_id", "instrument__symbol", "instrument__exchange")
        .annotate(
            wins=Count("pk", filter=Q(profit__gt=0)),
            losses=Count("pk", filter=Q(profit__lt=0)),
            flat=Count("pk", filter=Q(profit=0)),
            total=Sum("profit"),
        )
        .order_by("instrument__symbol", "instrument__exchange")
    )

    exits_by_account = defaultdict(list)
    for row in exits:
        exits_by_account[row["instrument__account_id"]].append(row)

    rows = []
    for account in accounts:
        invested = account.starting_cash + deposits.get(account.pk, ZERO)
        value = amount(
            account.cash + account.reserve
            + sum((item.value for item in account.instrument_set.all()), ZERO)
        )
        result = value - invested
        by_symbol = exits_by_account[account.pk]

        rows.append({
            "account": account,
            "value": value,
            "result": result,
            "return_pct": result / invested * HUNDRED if invested > ZERO else None,
            "realized": sum((item["total"] or ZERO for item in by_symbol), ZERO),
            "wins": sum(item["wins"] for item in by_symbol),
            "losses": sum(item["losses"] for item in by_symbol),
            "flat": sum(item["flat"] for item in by_symbol),
            "symbols": by_symbol,
        })
    return rows


def clean_history(history):
    """Convert stored daily closes to one positive price per date."""
    points = {}
    for row in history:
        try:
            raw_date, raw_price = row
            day = date.fromisoformat(raw_date)
            price = Decimal(raw_price)
            if price.is_finite() and price > ZERO:
                points[day] = price
        except (TypeError, ValueError, InvalidOperation):
            continue
    return points


def replay(bot, prices, start, end):
    """Test one strategy on one instrument with a normalized starting balance."""
    cash, units, entry_cost = ONE, ZERO, ZERO
    peak, max_drawdown = ONE, ZERO
    exits = 0

    for index in range(start, end):
        # A signal uses only earlier closes. Its order uses the next close.
        action, _ = signal_for(bot, prices[:index])
        price = prices[index]

        if action == "buy" and not units:
            entry_cost = cash
            units = cash / (price * (ONE + FEE))
            cash = ZERO
        elif action == "sell" and units:
            cash = units * price * (ONE - FEE)
            units = ZERO
            exits += 1

        # Include estimated closing fees in the value of an open position.
        equity = cash + units * price * (ONE - FEE)
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (ONE - equity / peak) * HUNDRED)

    return {
        "return_pct": (equity - ONE) * HUNDRED,
        "drawdown_pct": max_drawdown,
        "exits": exits,
    }


def evaluate(bot, price_series, start, end):
    results = [replay(bot, prices, start, end) for prices in price_series]
    return {
        "bot": bot,
        "label": BOT_LABELS[bot],
        "return_pct": sum((row["return_pct"] for row in results), ZERO) / len(results),
        "drawdown_pct": max(row["drawdown_pct"] for row in results),
        "exits": sum(row["exits"] for row in results),
    }


def learning_result(account):
    """Choose once on training data, then evaluate on later unseen dates."""
    histories = []
    included, skipped = [], []

    for item in account.instrument_set.exclude(group="option").order_by("symbol"):
        label = f"{item.symbol} ({item.exchange})" if item.exchange else item.symbol
        history = clean_history(item.history)
        if len(history) < MIN_HISTORY:
            skipped.append(label)
            continue
        histories.append(history)
        included.append(label)

    if not histories:
        return {
            "error": f"No instrument has {MIN_HISTORY} stored daily closes yet.",
            "skipped": skipped,
        }

    # All strategies and instruments are evaluated on the same trading dates.
    dates = sorted(set.intersection(*(set(history) for history in histories)))
    if len(dates) < MIN_HISTORY:
        return {
            "error": f"The selected instruments need {MIN_HISTORY} shared trading dates.",
            "skipped": skipped,
        }

    price_series = [[history[day] for day in dates] for history in histories]
    split = WARMUP + (len(dates) - WARMUP) * 2 // 3

    training = [
        evaluate(bot, price_series, WARMUP, split)
        for bot in BOT_LABELS
    ]
    training.sort(key=lambda row: row["return_pct"], reverse=True)

    baseline_training = next(row for row in training if row["bot"] == account.bot)
    candidate_training = next(row for row in training if row["bot"] != account.bot)
    baseline = evaluate(account.bot, price_series, split, len(dates))
    candidate = evaluate(candidate_training["bot"], price_series, split, len(dates))
    improvement = candidate["return_pct"] - baseline["return_pct"]

    if candidate_training["return_pct"] <= baseline_training["return_pct"]:
        decision = "The alternative had a lower or equal training return."
    elif min(baseline["exits"], candidate["exits"]) < MIN_VALIDATION_EXITS:
        decision = "There were fewer than three closed trades in at least one validation run."
    elif improvement < MIN_IMPROVEMENT:
        decision = "The validation return difference was below 0.50 percentage points."
    elif candidate["drawdown_pct"] > baseline["drawdown_pct"]:
        decision = "The alternative had a higher validation return and a larger drawdown."
    else:
        decision = f"Alternative with a higher validation return: {candidate['label']}."

    return {
        "included": included,
        "skipped": skipped,
        "training": training,
        "training_start": dates[WARMUP],
        "training_end": dates[split - 1],
        "validation_start": dates[split],
        "validation_end": dates[-1],
        "baseline": baseline,
        "candidate": candidate,
        "improvement": improvement,
        "decision": decision,
    }


@login_required
@never_cache
@require_GET
def strategy_comparison(request):
    rows = account_results(request.user)
    learning = None
    lab_account = None
    source = request.GET.get("learn")

    if source:
        try:
            account_id = int(source)
        except ValueError:
            raise Http404 from None
        if not 1 <= account_id <= 2**63 - 1:
            raise Http404

        # A user may analyze only a virtual account they own.
        lab_account = get_object_or_404(
            Account, pk=account_id, owner=request.user, mode="virtual"
        )
        learning = learning_result(lab_account)

    return render(request, "trading/page.html", {
        "title": "Strategy comparison",
        "strategy_comparison": True,
        "strategy_rows": rows,
        "learning": learning,
        "lab_account": lab_account,
                "trade_page": order_history(request),
    })