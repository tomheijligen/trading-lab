"""Historical replays use immutable inputs and no paper-account writes."""
from datetime import date, timedelta
from decimal import Decimal
from math import sqrt
from statistics import stdev

from django.db import transaction

from .engine import signal_for
from .models import BOTS, Simulation, ZERO

WARMUP = 30
BPS = Decimal("10000")


class SimulationError(ValueError):
    pass


def replay(bot, histories, start, end, capital, fee, impact):
    calendar = sorted({day for history in histories.values() for day in history if start <= day <= end})
    if not calendar or any(
        sum(day < start for day in history) < WARMUP
        or not any(start <= day <= end for day in history)
        for history in histories.values()
    ):
        raise SimulationError("Each instrument needs 30 earlier closes and data in the test period.")

    previous = {key: sorted(day for day in history if day < start)[-WARMUP:]
                for key, history in histories.items()}
    cash = {key: capital / len(histories) for key in histories}
    units = dict.fromkeys(histories, ZERO)
    marks = dict.fromkeys(histories, ZERO)
    costs = dict.fromkeys(histories, ZERO)
    equity = [[start.isoformat(), float(capital)]]
    fees, wins, exits, orders = ZERO, 0, 0, 0
    peak, drawdown = capital, ZERO
    returns = []
    last_value = capital

    for day in calendar:
        for key, history in histories.items():
            if day not in history:
                continue
            price = marks[key] = history[day]
            window = previous[key]
            action, _ = signal_for(bot, [history[earlier] for earlier in window])
            previous[key] = (window + [day])[-WARMUP:]
            if action == "buy" and not units[key] and cash[key] > ZERO:
                costs[key] = cash[key]
                gross = cash[key] / (1 + fee)
                fees += gross * fee
                units[key] = gross / (price * (1 + impact))
                cash[key] = ZERO
                orders += 1
            elif action == "sell" and units[key]:
                gross = units[key] * price * (1 - impact)
                fees += gross * fee
                cash[key] = gross * (1 - fee)
                units[key] = ZERO
                wins += cash[key] > costs[key]
                exits += 1
                orders += 1

        value = sum(cash.values()) + sum(units[key] * marks[key] for key in histories) * (1 - impact) * (1 - fee)
        peak = max(peak, value)
        drawdown = max(drawdown, (1 - value / peak) * 100)
        equity.append([day.isoformat(), float(value)])
        if last_value:
            returns.append(float(value / last_value - 1))
        last_value = value

    deviation = stdev(returns) if len(returns) >= 2 else 0
    observations_per_year = len(calendar) * 365.25 / ((end - start).days + 1)
    return {
        "gain": float(value - capital),
        "return_pct": float((value / capital - 1) * 100),
        "drawdown": float(drawdown),
        "orders": orders,
        "win_rate": 100 * wins / exits if exits else None,
        "fees": float(fees),
        "sharpe": (sum(returns) / len(returns) / deviation * sqrt(observations_per_year)
                   if deviation and len(calendar) >= 20 else None),
        "equity": equity,
    }


def calculate(job):
    histories = {
        key: {date.fromisoformat(day): Decimal(close) for day, close in rows.items()}
        for key, rows in job.inputs.items()
    }
    if not histories:
        raise SimulationError("Add instruments with stored EUR daily closes first.")
    fee = Decimal(job.fee_bps) / BPS
    impact = (Decimal(job.slippage_bps) + Decimal(job.spread_bps) / 2) / BPS

    def run(bot, start, end):
        return replay(bot, histories, start, end, job.capital, fee, impact)

    rows = [{"bot": label, **run(bot, job.start, job.end)} for bot, label in BOTS]
    validation = []
    span = (job.end - job.start).days + 1
    if span >= 90:
        segment = span // 3
        for fold in range(1, 3):
            boundary = job.start + timedelta(days=segment * fold)
            finish = job.end if fold == 2 else boundary + timedelta(days=segment - 1)
            try:
                winner = max(
                    BOTS,
                    key=lambda choice: run(choice[0], job.start, boundary - timedelta(days=1))["return_pct"],
                )
                report = run(winner[0], boundary, finish)
            except SimulationError:
                continue
            validation.append({
                "bot": winner[1], "start": boundary.isoformat(), "end": finish.isoformat(),
                "gain": report["gain"], "drawdown": report["drawdown"],
            })
    return {"strategies": rows, "validation": validation}


@transaction.atomic
def process_simulation():
    # A killed worker rolls back its claim. A second worker skips the locked job.
    job = Simulation.objects.select_for_update(skip_locked=True).filter(
        status="queued",
    ).order_by("pk").first()
    if job is None:
        return False
    try:
        job.result = calculate(job)
        job.status = "done"
    except SimulationError as error:
        job.error = str(error)
        job.status = "failed"
    job.save(update_fields=["result", "status", "error"])
    return True
