"""Daily OHLCV adapters using the shared provider client."""
import re
from datetime import UTC, date, datetime, time, timedelta

from django.utils import timezone

from .historical_data import BarInput
from .market_data import ALPACA, BINANCE, TWELVE, DataError, alpaca_headers, twelve_data

DAY = timedelta(days=1)
HISTORY_TTL = timedelta(minutes=5)
PAGE_DAYS = 90
ALPACA_PAGE_CAP = 8
BINANCE_PAIR_WEIGHT = 20
BINANCE_BAR_WEIGHT = 2
PROVIDERS = {"twelve": TWELVE, "alpaca": ALPACA, "binance": BINANCE}
ALPACA_MICS = {"XNYS", "XNAS", "XNGS", "XNMS", "XNCM", "ARCX", "XASE", "IEXG", "BATS"}
TWELVE_ADJUSTMENTS = {"raw": "none", "split": "splits", "dividend": "dividends", "all": "all"}


def instrument_identity(provider, asset_class, symbol, exchange, currency, adjustment):
    if provider not in PROVIDERS or asset_class not in {"stock", "etf", "crypto"}:
        raise ValueError("Unsupported provider or asset class.")
    symbol, exchange, currency = (value.strip().upper() for value in (symbol, exchange, currency))
    if asset_class == "crypto":
        if not re.fullmatch(r"[A-Z0-9]{1,16}/[A-Z0-9]{1,8}", symbol):
            raise ValueError("Use an explicit crypto pair in BASE/QUOTE format.")
        base, quote = symbol.split("/")
        if quote != currency or base == quote or not re.fullmatch(r"[A-Z0-9 -]{1,35}", exchange):
            raise ValueError("Specify the crypto venue and the pair's quote currency.")
        if adjustment != "raw":
            raise ValueError("Crypto backfill supports raw candles only.")
    elif not (
        re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,34}", symbol)
        and re.fullmatch(r"[A-Z][A-Z0-9]{3}", exchange)
        and re.fullmatch(r"[A-Z]{3}", currency)
    ):
        raise ValueError("Equity backfill requires a ticker, exchange MIC and three-letter currency.")

    if provider == "binance" and (asset_class != "crypto" or exchange != "BINANCE"):
        raise ValueError("Binance backfill requires a crypto pair on BINANCE.")
    if provider == "alpaca" and (
        asset_class == "crypto" or exchange not in ALPACA_MICS or currency != "USD"
    ):
        raise ValueError("Alpaca IEX backfill supports USD equities on selected US listing venues.")
    if adjustment not in TWELVE_ADJUSTMENTS:
        raise ValueError("Unsupported historical adjustment method.")
    return {
        "provider": PROVIDERS[provider].name, "asset_class": asset_class, "symbol": symbol,
        "exchange": exchange, "currency": currency, "adjustment": adjustment,
    }


def _object_rows(payload, key, *, nullable=False):
    if isinstance(payload, dict) and key in payload:
        rows = payload[key]
        if isinstance(rows, list):
            return rows
        if nullable and rows is None:
            return []
    raise DataError("Unexpected historical provider response structure.")


def _twelve_bars(identity, calendar, start, end, request):
    params = {
        "symbol": identity["symbol"], "interval": "1day", "order": "asc",
        "start_date": start.isoformat(), "end_date": (end + DAY).isoformat(),
        "adjust": TWELVE_ADJUSTMENTS[identity["adjustment"]],
    }
    crypto = identity["asset_class"] == "crypto"
    params["exchange" if crypto else "mic_code"] = identity["exchange"]
    payload = request(lambda: twelve_data("time_series", cache_ttl=HISTORY_TTL, **params))
    rows = _object_rows(payload, "values")
    meta = payload.get("meta")
    if (
        not isinstance(meta, dict)
        or str(meta.get("symbol", "")).upper() != identity["symbol"]
        or str(meta.get("exchange" if crypto else "mic_code", "")).upper() != identity["exchange"]
        or (not crypto and str(meta.get("currency", "")).upper() != identity["currency"])
        or meta.get("interval", "1day") != "1day"
    ):
        raise DataError("Twelve Data historical response does not match the requested series.")
    received_at = timezone.now()
    for row in rows:
        try:
            day = date.fromisoformat(row["datetime"])
            if start <= day <= end:
                yield BarInput(
                    day, calendar.close_at(day) or received_at,
                    row["open"], row["high"], row["low"], row["close"], row.get("volume"),
                )
        except (KeyError, TypeError, ValueError, OverflowError):
            yield None


def _alpaca_bars(identity, calendar, start, end, request):
    headers = alpaca_headers()
    if not headers:
        raise DataError("Set Alpaca market data credentials before applying a backfill.")
    symbol = identity["symbol"]
    # Alpaca labels daily equity bars in New York time. Query through local end-of-day.
    params = {
        "symbols": symbol, "feed": "iex", "timeframe": "1Day", "currency": "USD",
        "adjustment": identity["adjustment"], "asof": "-", "sort": "asc", "limit": 100,
        "start": datetime.combine(start, time.min, calendar.timezone).isoformat(),
        "end": datetime.combine(end, time.max, calendar.timezone).isoformat(),
    }
    seen = set()
    received_at = timezone.now()
    for _ in range(ALPACA_PAGE_CAP):
        payload = request(lambda: ALPACA.fetch("bars", params, headers, ttl=HISTORY_TTL))
        if not isinstance(payload, dict) or "bars" not in payload:
            raise DataError("Unexpected Alpaca historical response structure.")
        series = payload["bars"]
        if series is not None and (not isinstance(series, dict) or series.keys() - {symbol}):
            raise DataError("Alpaca historical response does not match the requested symbol.")
        rows = _object_rows(series, symbol, nullable=True) if series else []
        for row in rows:
            try:
                stamp = datetime.fromisoformat(row["t"])
                if stamp.utcoffset() is None:
                    raise ValueError("Missing offset.")
                day = stamp.astimezone(calendar.timezone).date()
                if start <= day <= end:
                    yield BarInput(
                        day, calendar.close_at(day) or received_at,
                        row["o"], row["h"], row["l"], row["c"], row.get("v"),
                    )
            except (KeyError, TypeError, ValueError, OverflowError):
                yield None
        token = payload.get("next_page_token")
        if token is None:
            return
        if not isinstance(token, str) or not token or token in seen:
            raise DataError("Alpaca returned an invalid or repeated historical page token.")
        seen.add(token)
        params["page_token"] = token
    raise DataError("Alpaca historical pagination exceeded the per-window limit.")


def verify_binance_pair(identity, request):
    base, quote = identity["symbol"].split("/")
    symbol = base + quote
    payload = request(lambda: BINANCE.fetch(
        "exchangeInfo", {"symbol": symbol}, {}, ttl=timedelta(hours=24), weight=BINANCE_PAIR_WEIGHT,
    ))
    rows = _object_rows(payload, "symbols")
    if len(rows) != 1 or not isinstance(rows[0], dict) or (
        rows[0].get("symbol"), rows[0].get("baseAsset"),
        rows[0].get("quoteAsset"), rows[0].get("isSpotTradingAllowed"),
    ) != (symbol, base, quote, True):
        raise DataError("Binance symbol does not match the requested Spot pair.")


def _milliseconds(day):
    return int(datetime.combine(day, time.min, UTC).timestamp()) * 1000


def _binance_bars(identity, calendar, start, end, request):
    params = {
        "symbol": identity["symbol"].replace("/", ""), "interval": "1d", "timeZone": "0",
        "startTime": _milliseconds(start), "endTime": _milliseconds(end + DAY) - 1,
        "limit": PAGE_DAYS,
    }
    rows = request(lambda: BINANCE.fetch(
        "klines", params, {}, ttl=HISTORY_TTL, weight=BINANCE_BAR_WEIGHT,
    ))
    if not isinstance(rows, list):
        raise DataError("Unexpected Binance historical response structure.")
    for row in rows:
        try:
            day = datetime.fromtimestamp(row[0] // 1000, UTC).date()
            if start <= day <= end:
                if row[0] != _milliseconds(day) or row[6] != _milliseconds(day + DAY) - 1:
                    raise ValueError("Invalid UTC candle boundaries.")
                yield BarInput(day, calendar.close_at(day), *row[1:6])
        except (IndexError, KeyError, TypeError, ValueError, OSError, OverflowError):
            yield None


FETCH_BARS = {"twelve": _twelve_bars, "alpaca": _alpaca_bars, "binance": _binance_bars}
