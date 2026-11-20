import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Account, Trade, ZERO
from .performance import record_capital


CENT = Decimal("0.01")
TICK = Decimal("0.00000001")
UNIT = Decimal("0.0000000001")
FEE = Decimal("0.002")


class DataError(Exception):
    pass


@dataclass(frozen=True)
class Snapshot:
    history: list
    currency: str
    time: datetime
    price: Decimal
    market_open: bool
    name: str = ""

    @property
    def bar(self):
        return date.fromisoformat(self.history[-1][0])


def amount(value):
    return value.quantize(CENT)


def source_number(value):
    number = Decimal(str(value))
    if not number.is_finite() or not ZERO < number <= Decimal("1000000000"):
        raise ValueError("Invalid price.")
    return number


def signal_for(bot, closes):
    """Read completed daily closes in order from oldest to newest."""
    minimum = {
        "trend": 20,
        "cross": 30,
        "momentum": 11,
        "breakout": 21,
        "reversion": 21,
        "rsi": 15,
        "voltrend": 21,
        "pullback": 21,
        "updays": 11,
        "hold": 2,
    }[bot]

    if len(closes) < minimum:
        return "hold", f"Waiting for at least {minimum} completed daily closes."

    latest = closes[-1]

    if bot == "rsi":
        window = closes[-15:]
        moves = [current - previous for previous, current in zip(window, window[1:])]
        gains = sum((move for move in moves if move > ZERO), ZERO)
        losses = sum((-move for move in moves if move < ZERO), ZERO)
        movement = gains + losses
        rsi = Decimal("100") * gains / movement if movement else Decimal("50")
        reason = f"14-move RSI {rsi:.2f}. Buy at 30 or below; sell at 70 or above."
        if rsi <= 30:
            return "buy", reason
        if rsi >= 70:
            return "sell", reason
        return "hold", reason

    if bot == "voltrend":
        window = closes[-21:]
        reference = sum(window[:-1]) / 20
        average_move = sum(
            abs(current / previous - 1)
            for previous, current in zip(window, window[1:])
        ) / 20 * 100

        reason = (
            f"Close {latest:.4f} versus prior 20-day average {reference:.4f}. "
            f"Mean absolute daily move {average_move:.2f}%; entry cap 2.00%."
        )
        if latest < reference:
            return "sell", reason
        if latest > reference and average_move <= 2:
            return "buy", reason
        return "hold", reason

    if bot == "pullback":
        long_average = sum(closes[-21:-1]) / 20
        previous_close = closes[-2]
        previous_short_average = sum(closes[-7:-2]) / 5
        short_average = sum(closes[-6:-1]) / 5

        reason = (
            f"Close {latest:.4f}; prior 5-day average {short_average:.4f}; "
            f"prior 20-day average {long_average:.4f}. "
            f"Previous close {previous_close:.4f} vs its 5-day reference "
            f"{previous_short_average:.4f}."
        )
        if latest < long_average:
            return "sell", reason
        if (
            previous_close <= previous_short_average
            and latest > short_average
            and latest > long_average
        ):
            return "buy", reason
        return "hold", reason

    if bot == "updays":
        window = closes[-11:]
        pairs = list(zip(window, window[1:]))
        up = sum(current > previous for previous, current in pairs)
        down = sum(current < previous for previous, current in pairs)
        reason = f"Last 10 moves: {up} up, {down} down. Signal threshold: 7."
        if up >= 7:
            return "buy", reason
        if down >= 7:
            return "sell", reason
        return "hold", reason

    if bot == "hold":
        # The order engine skips buys while a position is open.
        return "buy", "Benchmark: buy once when eligible and hold the position."

    if bot == "breakout":
        entry_level = max(closes[-21:-1])
        exit_level = min(closes[-11:-1])
        reason = (
            f"Close {latest:.4f}. Entry above {entry_level:.4f}; "
            f"exit below {exit_level:.4f}."
        )
        if latest > entry_level:
            return "buy", reason
        if latest < exit_level:
            return "sell", reason
        return "hold", reason

    if bot == "reversion":
        previous_average = sum(closes[-21:-1]) / 20
        entry_level = previous_average * Decimal("0.97")
        reason = (
            f"Close {latest:.4f}. Entry at or below {entry_level:.4f}; "
            f"exit at or above average {previous_average:.4f}."
        )
        if latest <= entry_level:
            return "buy", reason
        if latest >= previous_average:
            return "sell", reason
        return "hold", reason

    if bot == "trend":
        left, right = latest, sum(closes[-20:]) / 20
        reason = f"Latest close {left:.4f} versus 20-day average {right:.4f}."
    elif bot == "cross":
        left, right = sum(closes[-10:]) / 10, sum(closes[-30:]) / 30
        reason = f"10-day average {left:.4f} versus 30-day average {right:.4f}."
    else:
        left, right = latest, closes[-11]
        change = (left / right - 1) * 100
        reason = f"10-day return {change:+.2f}% ({right:.4f} to {left:.4f})."

    signal = "buy" if left > right else "sell" if left < right else "hold"
    return signal, reason


def request_data(endpoint, **params):
    api_key = os.getenv("TWELVE_DATA_API_KEY")
    if not api_key:
        raise DataError("Set TWELVE_DATA_API_KEY to fetch market data.")

    request = Request(
        f"https://api.twelvedata.com/{endpoint}?{urlencode(params)}",
        headers={"Authorization": f"apikey {api_key}"},
    )
    try:
        with urlopen(request, timeout=6) as response:
            result = json.loads(response.read(500_000))
    except (URLError, TimeoutError, ValueError):
        raise DataError("The market data request failed.") from None

    if not isinstance(result, dict) or result.get("status") == "error":
        raise DataError("Check the symbol, exchange, and data permissions.")
    return result


def market_snapshot(item, fx):
    params = {"symbol": item.symbol}
    if item.exchange:
        params["exchange"] = item.exchange

    series = request_data("time_series", **params, interval="1day", outputsize=121)
    quote = request_data("quote", **params, interval="1min")

    try:
        # Skip the newest daily bar because it can still be in progress.
        rows = sorted(series["values"], key=lambda row: row["datetime"])
        history = [
            [
                date.fromisoformat(row["datetime"]).isoformat(),
                str(source_number(row["close"])),
            ]
            for row in rows[:-1]
        ]
        if not history:
            raise ValueError("No completed bars.")

        currency = series["meta"].get("currency")
        if item.group == "crypto":
            currency = item.symbol.rsplit("/", 1)[-1]
        if not currency or len(currency) != 3 or not currency.isascii() or not currency.isupper():
            raise ValueError("Unsupported quote currency.")

        if currency not in fx:
            exchange_rate = request_data("exchange_rate", symbol=f"{currency}/EUR")
            fx[currency] = source_number(exchange_rate["rate"])

        price = source_number(
            source_number(quote["close"]) * fx[currency]
        ).quantize(TICK)
        if price <= ZERO:
            raise ValueError("Price below supported precision.")

        quote_timestamp = quote.get("last_quote_at") or quote["timestamp"]
        raw_name = quote.get("name")
        name = raw_name.strip()[:255] if isinstance(raw_name, str) else ""
        return Snapshot(
            history=history,
            currency=currency,
            time=datetime.fromtimestamp(int(quote_timestamp), tz=UTC),
            price=price,
            market_open=quote.get("is_market_open") is True,
            name=name,
        )
    except (
        AttributeError, KeyError, TypeError, ValueError,
        InvalidOperation, OSError, OverflowError,
    ):
        raise DataError(
            "The provider returned incomplete or invalid market data."
        ) from None


def record_trade(item, account, side, price, units, fee, reason, profit=None):
    Trade.objects.create(
        instrument=item,
        bot=account.bot,
        side=side,
        price=price,
        units=units,
        fee=fee,
        reason=reason,
        profit=profit,
    )


# Rotate a small set of candidates to limit quote requests.
DISCOVERY_UNIVERSE = {
    "stock": (("AAPL", "NASDAQ"), ("MSFT", "NASDAQ"), ("NVDA", "NASDAQ")),
    "etf": (("SPY", "NYSE ARCA"), ("QQQ", "NASDAQ")),
    "crypto": (("BTC/USD", ""), ("ETH/USD", "")),
}
DISCOVERY_WATCHLIST_TARGET = 3


def select_round_instruments(account, today):
    """Keep manual instruments and open positions, then discover candidates."""
    tradable = account.instrument_set.exclude(group="option").order_by("pk")
    selected = list(tradable.filter(Q(auto_discovered=False) | Q(units__gt=ZERO)))

    if not account.auto_discovery:
        return selected

    covered = {item.group for item in selected}
    slots = max(0, DISCOVERY_WATCHLIST_TARGET - len(selected))
    day_number = today.toordinal()

    for group, candidates in DISCOVERY_UNIVERSE.items():
        if not slots:
            break
        if group in covered or getattr(account, group) <= ZERO:
            continue

        symbol, exchange = candidates[day_number % len(candidates)]
        item, _ = account.instrument_set.get_or_create(
            symbol=symbol,
            exchange=exchange,
            defaults={"group": group, "auto_discovered": True},
        )
        if item.group != group:
            continue

        selected.append(item)
        covered.add(group)
        slots -= 1

    return selected


def candidate_label(items):
    symbols = ", ".join(item.symbol for item in items)
    return f" Scanned: {symbols}." if symbols else " No tradable instruments selected."


def run_account(account_id, *, execute, scheduled=False, market_cache=None):
    # Select instruments and claim the cooldown under an account lock.
    with transaction.atomic():
        account = Account.objects.select_for_update().get(pk=account_id)
        if account.mode != "virtual" or not account.owner.is_active:
            raise DataError("This account cannot run a virtual round.")
        if scheduled and not account.automatic:
            return "Automatic rounds are paused."

        now = timezone.now()
        if account.last_attempt and now - account.last_attempt < timedelta(minutes=2):
            return "Please wait two minutes between market requests for this account."

        account.last_attempt = now
        account.save(update_fields=["last_attempt"])
        items = select_round_instruments(account, now.date())
        round_ids = [item.pk for item in items]

    # Fetch quotes outside the transaction. Scheduled accounts share the cache.
    scanned = candidate_label(items)
    fx = {"EUR": Decimal("1")}
    snapshots = {}
    errors = []
    if market_cache is None:
        market_cache = {}

    for item in items:
        cache_key = (item.symbol, item.exchange, item.group)
        if cache_key not in market_cache:
            try:
                market_cache[cache_key] = (market_snapshot(item, fx), None)
            except DataError as error:
                market_cache[cache_key] = (None, str(error))

        snapshot, error = market_cache[cache_key]
        if error is not None:
            errors.append(f"{item.symbol}: {error}")
        else:
            snapshots[item.pk] = snapshot

    # Recheck the account and save quotes, orders, and statuses atomically.
    with transaction.atomic():
        account = Account.objects.select_for_update().get(pk=account_id)
        if account.mode != "virtual":
            raise DataError("This account cannot run a virtual round.")
        if not get_user_model().objects.filter(
            pk=account.owner_id, is_active=True
        ).exists():
            raise DataError("This user is disabled.")
        if scheduled and not account.automatic:
            return "Automatic rounds are paused."

        items = list(
            account.instrument_set.exclude(group="option")
            .filter(Q(pk__in=round_ids) | Q(units__gt=ZERO))
            .order_by("pk")
        )
        now = timezone.now()
        eligible = []
        execution = {}

        for item in items:
            snapshot = snapshots.get(item.pk)
            if snapshot is None:
                if execute:
                    execution[item.pk] = "No quote received for this round"
                continue

            if item.quote_time and item.quote_time > snapshot.time:
                if execute:
                    execution[item.pk] = "Quote ignored: newer quote already stored"
                continue

            item.history = snapshot.history
            item.currency = snapshot.currency
            item.source = "Market provider"
            closes = [Decimal(row[1]) for row in snapshot.history]
            item.signal, item.explanation = signal_for(account.bot, closes)
            item.price = snapshot.price
            item.quote_time = snapshot.time
            if snapshot.name and snapshot.name.casefold() != item.symbol.casefold():
                item.display_name = snapshot.name
            item.save()

            age = (now - snapshot.time).total_seconds()
            if item.group != "crypto" and not snapshot.market_open:
                reason = "Market closed or opening status unavailable"
            elif age < 0:
                reason = "Quote timestamp is ahead of the server clock"
            elif age > 1800:
                reason = "Quote too old: older than 30 minutes"
            else:
                eligible.append((item, snapshot))
                continue

            if execute:
                execution[item.pk] = reason

        count = 0
        if execute:
            count = apply_signals(account, items, eligible, execution=execution)
            checked_items = []
            for item in items:
                if item.pk in execution:
                    item.execution_status = execution[item.pk]
                    item.execution_checked_at = now
                    checked_items.append(item)

            if checked_items:
                account.instrument_set.bulk_update(
                    checked_items,
                    ["execution_status", "execution_checked_at"],
                )

        if snapshots:
            record_capital(account)

        summary = f"{len(snapshots)} quotes received. {count} simulated orders."
        summary += scanned
        if errors:
            summary += " " + " ".join(errors)
        return summary


def apply_signals(account, items, eligible, *, execution=None):
    """Execute simulated orders and explain what happened for each instrument."""
    if execution is None:
        execution = {}

    count = 0
    sold_this_round = set()

    # Process exits first so the proceeds are available for later buys.
    for item, snap in eligible:
        if item.stop_bar and item.signal == "sell" and snap.bar > item.stop_bar:
            item.stop_bar = None

        if not item.units:
            item.save(update_fields=["stop_bar"])
            continue

        item.peak = max(item.peak, snap.price)
        gross = amount(item.units * snap.price)
        fee = amount(gross * FEE)
        net = gross - fee
        reason = ""

        if item.stop and net <= item.cost * (100 - item.stop) / 100:
            reason = f"Fixed stop ({item.stop}%)."
        elif (
            item.trailing
            and snap.price <= item.peak * (100 - item.trailing) / 100
        ):
            reason = f"Trailing stop ({item.trailing}%). Peak EUR {item.peak}."

        if reason:
            item.stop_bar = snap.bar
        elif item.signal == "sell" and item.entry_bar and snap.bar > item.entry_bar:
            reason = f"Strategy sell. {item.explanation}"

        if reason:
            record_trade(
                item, account, "Sell", snap.price, item.units, fee,
                reason, net - item.cost,
            )
            account.cash += net
            item.units = item.cost = item.peak = ZERO
            item.exit_bar = snap.bar
            sold_this_round.add(item.pk)
            execution[item.pk] = "Order placed: SELL"
            count += 1

        item.save()

    all_quotes_eligible = len(eligible) == len(items)

    # Preserve the existing requirement that all selected quotes qualify for buys.
    for item, snap in eligible:
        if item.pk in sold_this_round:
            continue
        if item.signal == "hold":
            execution[item.pk] = "Hold: strategy is waiting"
            continue
        if item.signal == "sell":
            if not item.units:
                execution[item.pk] = "No position to sell"
            elif item.entry_bar is None:
                execution[item.pk] = "Sell blocked: entry date unavailable"
            else:
                execution[item.pk] = "Sell blocked: waiting for a later signal bar"
            continue
        if item.signal != "buy":
            execution[item.pk] = "No actionable signal"
            continue
        if item.units:
            execution[item.pk] = "Already holding this instrument"
            continue
        if item.stop_bar:
            execution[item.pk] = "Buy blocked: waiting for a new setup after stop-loss"
            continue
        if item.exit_bar and snap.bar <= item.exit_bar:
            execution[item.pk] = "Buy blocked: waiting for a later signal bar"
            continue

        allocation = getattr(account, item.group)
        if allocation <= ZERO:
            execution[item.pk] = "No allocation available for this asset class"
            continue
        if not all_quotes_eligible:
            execution[item.pk] = (
                "Buy blocked: another selected instrument has no eligible quote"
            )
            continue

        group = [peer for peer in items if peer.group == item.group]
        equity = account.cash + sum(peer.value for peer in items)
        target = equity * allocation / 100
        remaining = target - sum(peer.value for peer in group)
        budget = min(account.cash, target / len(group), remaining)

        if budget < 1:
            execution[item.pk] = (
                "Insufficient trading cash"
                if account.cash < 1
                else "No allocation available for this order"
            )
            continue

        units = (budget / (1 + FEE) / snap.price).quantize(
            UNIT, rounding=ROUND_DOWN
        )
        gross = amount(units * snap.price)
        fee = amount(gross * FEE)

        if gross <= ZERO or gross + fee > budget:
            execution[item.pk] = "Buy blocked: amount is below supported order precision"
            continue

        account.cash -= gross + fee
        item.units = units
        item.cost = gross + fee
        item.peak = snap.price
        item.entry_bar = snap.bar
        item.save()

        record_trade(
            item, account, "Buy", snap.price, units, fee,
            f"Strategy buy. {item.explanation}",
        )
        execution[item.pk] = "Order placed: BUY"
        count += 1

    account.save(update_fields=["cash"])
    return count