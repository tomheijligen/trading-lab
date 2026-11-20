"""Latest completed Binance minute candles shared between processes."""
import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from .models import MarketCache


def stream_key(symbol):
    return hashlib.sha256(f"binance-stream:{symbol}".encode()).hexdigest()


def save_candle(message, allowed):
    try:
        data = message["data"]["k"]
        symbol = data["s"]
        if symbol not in allowed or data["i"] != "1m" or data["x"] is not True:
            return False
        timestamp = datetime.fromtimestamp((data["T"] + 1) / 1000, UTC)
        price = Decimal(data["c"])
        now = timezone.now()
        if not price.is_finite() or not 0 < price <= 1_000_000_000:
            return False
        if not now - timedelta(minutes=3) <= timestamp <= now:
            return False
    except (KeyError, TypeError, ValueError, OverflowError, OSError, InvalidOperation):
        return False

    with transaction.atomic():
        row, _ = MarketCache.objects.select_for_update().get_or_create(
            key=stream_key(symbol), defaults={"provider": "Binance stream"},
        )
        if row.fetched_at and row.fetched_at >= timestamp:
            return False
        row.payload = {"close": str(price)}
        row.fetched_at = timestamp
        row.save(update_fields=["payload", "fetched_at"])
    return True


def latest_stream_candle(symbol):
    now = timezone.now()
    row = MarketCache.objects.filter(
        key=stream_key(symbol),
        fetched_at__range=(now - timedelta(minutes=3), now),
    ).first()
    return (row.fetched_at, Decimal(row.payload["close"])) if row else None
