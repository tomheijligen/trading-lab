"""Research watchlist, independent of customer account instruments."""
import json
import math
import os
import re
from datetime import timedelta
from statistics import pstdev
from types import SimpleNamespace

from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from .engine import DataError, market_snapshot
from .models import MarketObservation
from .strategy_lab import clean_history


def scanner_universe():
    try:
        entries = json.loads(os.getenv("MARKET_SCANNER_UNIVERSE", "[]"))
        if not isinstance(entries, list) or len(entries) > 100:
            raise ValueError
        keys = set()
        for entry in entries:
            group, symbol, venue = entry["group"], entry["symbol"], entry["exchange"]
            if group not in {"stock", "etf", "crypto"}:
                raise ValueError
            if not re.fullmatch(r"[A-Z0-9./_-]{1,35}", symbol):
                raise ValueError
            if not re.fullmatch(r"[A-Z0-9 ._-]{0,35}", venue):
                raise ValueError
            keys.add((group, symbol, venue))
        return sorted(keys)
    except (KeyError, TypeError, ValueError) as error:
        raise ImproperlyConfigured("Invalid MARKET_SCANNER_UNIVERSE JSON.") from error


def refresh_scanner(universe=None):
    from decimal import Decimal

    fx = {"EUR": Decimal(1)}
    for group, symbol, venue in scanner_universe() if universe is None else universe:
        try:
            market_snapshot(SimpleNamespace(group=group, symbol=symbol, exchange=venue), fx)
        except DataError as error:
            yield f"{symbol} [{venue}]: {error}"


def rank_observations(universe, weights):
    now = timezone.now()
    watchlist = set(universe)
    results = []
    rows = MarketObservation.objects.filter(symbol__in={key[1] for key in watchlist})
    for row in rows:
        if (row.group, row.symbol, row.requested_exchange) not in watchlist:
            continue
        closes = [float(price) for _, price in sorted(clean_history(row.history).items())][-21:]
        if len(closes) < 21:
            continue
        momentum = (closes[-1] / closes[-11] - 1) * 100
        historical_return = (closes[-1] / closes[0] - 1) * 100
        trend = (closes[-1] / (sum(closes[-20:]) / 20) - 1) * 100
        volatility = pstdev(math.log(right / left) for left, right in zip(closes, closes[1:])) * 100
        score = momentum * weights[0] + trend * weights[1] - volatility * weights[2]
        age = now - row.quote_time
        results.append({
            "row": row, "momentum": momentum, "trend": trend, "volatility": volatility,
            "return": historical_return, "score": score,
            "age_minutes": int(age.total_seconds() / 60),
            "fresh": timedelta(0) <= age <= timedelta(minutes=30) and row.market_open,
        })
    return sorted(results, key=lambda row: (row["fresh"], row["score"]), reverse=True)
