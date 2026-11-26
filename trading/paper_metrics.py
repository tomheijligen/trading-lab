"""Paper measurements from stored account marks and executed orders."""
from datetime import timedelta
from math import sqrt
from statistics import stdev

from django.db.models import Window
from django.db.models.functions import RowNumber, TruncDate
from django.utils import timezone

from .models import Trade, ZERO


def paper_windows(account):
    now = timezone.now()
    ticks = list(account.capital_snapshots.filter(captured_at__gte=now - timedelta(days=94))
                 .annotate(day=TruncDate("captured_at")).annotate(
                     daily_rank=Window(RowNumber(), partition_by=["day"], order_by="-captured_at"),
                 ).filter(daily_rank=1).order_by("captured_at"))
    deposits = list(account.deposit_set.filter(booked=True, created__gte=now - timedelta(days=94))
                    .order_by("created").values_list("created", "amount"))
    orders = list(Trade.objects.filter(
        instrument__account=account, created__gte=now - timedelta(days=94),
    ).order_by("created").values("created", "profit", "fee", "bot"))
    switches = list(account.botswitch_set.filter(created__gte=now - timedelta(days=94))
                    .order_by("created").values("created", "previous_bot", "new_bot"))
    results = []
    for days in (7, 30, 90):
        cutoff = now - timedelta(days=days)
        baseline = next((tick for tick in reversed(ticks) if tick.captured_at <= cutoff), None)
        if (
            baseline is None or not ticks or cutoff - baseline.captured_at > timedelta(days=4)
            or now - ticks[-1].captured_at > timedelta(days=4) or baseline.pk == ticks[-1].pk
        ):
            results.append({"days": days, "available": False})
            continue
        marks = [tick for tick in ticks if tick.captured_at >= baseline.captured_at]
        end = marks[-1]
        flows = [(at, value) for at, value in deposits if baseline.captured_at < at <= end.captured_at]
        trades = [order for order in orders if baseline.captured_at < order["created"] <= end.captured_at]
        exits = [order for order in trades if order["profit"] is not None]
        change = end.value - baseline.value - sum((value for _, value in flows), ZERO)
        returns = []
        peak = index = 1.0
        drawdown = 0.0
        for left, right in zip(marks, marks[1:]):
            if left.value <= ZERO:
                continue
            flow = sum((value for at, value in flows if left.captured_at < at <= right.captured_at), ZERO)
            rate = float((right.value - left.value - flow) / left.value)
            returns.append(rate)
            index *= max(0.0, 1 + rate)
            peak = max(peak, index)
            drawdown = max(drawdown, (1 - index / peak) * 100)
        elapsed = max((end.captured_at - baseline.captured_at).total_seconds() / 86400, 1)
        deviation = stdev(returns) if len(returns) >= 20 else 0
        transitions = [event for event in switches if baseline.captured_at < event["created"] <= end.captured_at]
        first_after = next((event for event in switches if event["created"] > end.captured_at), None)
        strategy = first_after["previous_bot"] if first_after else account.bot
        results.append({
            "days": days, "available": True, "start": baseline.captured_at, "end": end.captured_at,
            "gain": change, "return_pct": (index - 1) * 100 if returns else None,
            "drawdown": drawdown if returns else None, "orders": len(trades),
            "fees": sum((order["fee"] for order in trades), ZERO),
            "win_rate": 100 * sum(order["profit"] > ZERO for order in exits) / len(exits) if exits else None,
            "strategy": "Multiple strategies" if transitions else strategy,
            "sharpe": sum(returns) / len(returns) / deviation * sqrt(len(returns) * 365.25 / elapsed)
                      if deviation else None,
        })
    return results


def strategy_periods(account):
    switches = list(account.botswitch_set.order_by("created", "pk"))
    first_bot = switches[0].previous_bot if switches else account.bot
    starts = [(None, first_bot)] + [(event.created, event.new_bot) for event in switches]
    ends = [event.created for event in switches] + [None]
    periods = []
    for (start, bot), end in list(zip(starts, ends))[-30:]:
        orders = Trade.objects.filter(instrument__account=account, bot=bot)
        if start:
            orders = orders.filter(created__gte=start)
        if end:
            orders = orders.filter(created__lt=end)
        rows = list(orders.values_list("profit", "fee"))
        periods.append({
            "start": start, "end": end, "bot": bot, "orders": len(rows),
            "profit": sum((profit for profit, _ in rows if profit is not None), ZERO),
            "fee": sum((fee for _, fee in rows), ZERO),
        })
    return list(reversed(periods))


def matching_paper_reference(account, start, end):
    def daily_close(day):
        return account.capital_snapshots.filter(captured_at__date=day).order_by(
            "-captured_at", "-pk",
        ).first()

    left, right = daily_close(start - timedelta(days=1)), daily_close(end)
    if not left or not right or not 1 <= left.value <= 10_000_000:
        return {}
    window = {"created__gt": left.captured_at, "created__lte": right.captured_at}
    if account.deposit_set.filter(booked=True, **window).exists() or account.botswitch_set.filter(**window).exists():
        return {}
    next_switch = account.botswitch_set.filter(created__gt=right.captured_at).order_by("created", "pk").first()
    return {
        "capital": str(left.value), "gain": str(right.value - left.value),
        "start": left.captured_at.isoformat(), "end": right.captured_at.isoformat(),
        "bot": next_switch.previous_bot if next_switch else account.bot,
    }
