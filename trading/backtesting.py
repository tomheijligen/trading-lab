"""Historical replays use immutable inputs and no paper-account writes."""
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from math import sqrt
from statistics import stdev
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils.timezone import is_aware

from .engine import signal_for
from .models import BOTS, Simulation, ZERO
from .simulation_data import OHLCV_FILL_MODEL, OHLCV_SCHEMA

WARMUP = 30
BPS = Decimal("10000")


class SimulationError(ValueError):
    pass


def replay(bot, histories, start, end, capital, fee, impact, *, execution=None):
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
            if execution is None:
                window = previous[key]
                previous[key] = (window + [day])[-WARMUP:]
            else:
                price, opening, _ = execution[day]
                window = sorted(
                    earlier for earlier in history
                    if earlier < day and execution[earlier][2] < opening
                )[-WARMUP:]
                if len(window) < WARMUP:
                    continue
            action, _ = signal_for(bot, [history[earlier] for earlier in window])
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


def _price(value):
    if isinstance(value, bool):
        raise ValueError("Invalid price")
    price = Decimal(value)
    if not price.is_finite() or price <= ZERO or not -100 <= price.adjusted() <= 100:
        raise ValueError("Invalid price")
    return price


def _parse_inputs(inputs):
    if not isinstance(inputs, dict) or not inputs:
        raise ValueError("Missing price history")
    if inputs.get("schema") != OHLCV_SCHEMA:
        return False, {
            key: {date.fromisoformat(day): _price(close) for day, close in rows.items()}
            for key, rows in inputs.items() if isinstance(rows, dict)
        }, None

    series = inputs["series"]
    bars = inputs["bars"]
    if (
        not isinstance(bars, dict) or not bars
        or not isinstance(series, dict) or series.get("currency") != "EUR"
        or any(not isinstance(series.get(key), str) or not series[key].strip()
               for key in ("provider", "asset_class", "symbol", "exchange", "adjustment"))
        or not isinstance(inputs["calendar"], dict)
        or inputs["fill_model"] != OHLCV_FILL_MODEL
    ):
        raise ValueError("Invalid historical series or bars")
    closes, execution = {}, {}
    venue_timezone = ZoneInfo(inputs["calendar"]["timezone"])
    previous_close = None
    previous_day = None
    for day, bar in sorted(bars.items()):
        session = date.fromisoformat(day)
        opening = datetime.fromisoformat(bar["opens_at"])
        closing = datetime.fromisoformat(bar["available_at"])
        if (
            not is_aware(opening) or not is_aware(closing) or opening >= closing
            or opening.astimezone(venue_timezone).date() != session
            or session in execution or (previous_close is not None and opening < previous_close)
            or (series["asset_class"] == "crypto" and previous_day is not None
                and session - previous_day != timedelta(days=1))
        ):
            raise ValueError("Invalid historical session timing")
        session_open, session_close = _price(bar["open"]), _price(bar["close"])
        if _price(bar["low"]) > min(session_open, session_close) or _price(bar["high"]) < max(
            session_open, session_close,
        ):
            raise ValueError("Invalid historical OHLC order")
        volume = Decimal(bar["volume"]) if bar["volume"] is not None else None
        if volume is not None and (not volume.is_finite() or volume < ZERO):
            raise ValueError("Invalid historical volume")
        closes[session] = session_close
        execution[session] = (session_open, opening, closing)
        previous_close = closing
        previous_day = session
    return True, {series["symbol"]: closes}, execution


def calculate(job):
    try:
        historical, histories, execution = _parse_inputs(job.inputs)
        if not histories or (not historical and len(histories) != len(job.inputs)):
            raise ValueError("Invalid price history")
        capital = _price(job.capital)
        if job.start > job.end or any(
            not 0 <= cost <= 500 for cost in (job.fee_bps, job.spread_bps, job.slippage_bps)
        ):
            raise ValueError("Invalid simulation parameters")
        gain = Decimal(job.paper_reference["gain"]) if job.paper_reference else None
        if gain is not None and not gain.is_finite():
            raise ValueError("Invalid paper reference")
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
        raise SimulationError("Invalid stored simulation input: check the series, bars, timing or parameters.") from None
    fee = Decimal(job.fee_bps) / BPS
    impact = (Decimal(job.slippage_bps) + Decimal(job.spread_bps) / 2) / BPS

    def run(bot, start, end):
        return replay(bot, histories, start, end, capital, fee, impact, execution=execution)

    rows = [{"bot": label, **run(bot, job.start, job.end)} for bot, label in BOTS]
    if gain is not None:
        for row in rows:
            row["difference"] = float(Decimal(str(row["gain"])) - gain)
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
    result = {"strategies": rows, "validation": validation}
    if historical:
        result["historical_data"] = {
            key: job.inputs[key] for key in ("schema", "series", "calendar", "fill_model")
        }
    return result


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
        job.result = {}
        job.error = str(error)
        job.status = "failed"
    job.save(update_fields=["result", "status", "error"])
    return True
