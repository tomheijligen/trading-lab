"""Indicator calculations for one validated, calendar-ordered daily series."""
from dataclasses import dataclass
from datetime import date, timedelta
from math import log
from statistics import pstdev


LABELS = {
    "price": "Last close", "volume": "Day volume", "relative_volume": "Relative volume",
    "return_10": "10-session return", "return_20": "20-session return",
    "trend": "Distance to SMA20", "sma20": "SMA20", "sma50": "SMA50", "sma200": "SMA200",
    "rsi": "RSI14", "macd": "MACD", "macd_signal": "MACD signal", "macd_histogram": "MACD histogram",
    "atr": "ATR14", "range": "Day high–low", "volatility": "Close volatility",
    "high_52": "52-week high", "low_52": "52-week low",
}
PERCENT_FIELDS = {"return_10", "return_20", "trend", "range", "volatility"}


@dataclass(frozen=True)
class Metric:
    value: float | None
    unit: str
    as_of: date | None
    source: str
    status: str


def _average(values):
    return sum(values) / len(values)


def _smooth(values, period, alpha):
    if len(values) < period:
        return []
    levels = [_average(values[:period])]
    for value in values[period:]:
        levels.append(levels[-1] + alpha * (value - levels[-1]))
    return levels


def calculate_indicators(sessions, bars, *, provider, currency, asset_class, symbol, calendar_known=True):
    """Use the contiguous valid suffix ending at the latest usable candle."""
    volume_unit = symbol.partition("/")[0] if asset_class == "crypto" else "shares"
    units = {key: "%" for key in PERCENT_FIELDS}
    units.update(volume=volume_unit, relative_volume="×", rsi="index")
    tail = []
    for day in reversed(sessions):
        bar = bars.get(day)
        if bar is None:
            if tail:
                break
        else:
            tail.append(bar)
    tail.reverse()
    as_of = tail[-1].session_date if tail else None
    status = "unknown_calendar" if not calendar_known else "missing"
    result = {key: Metric(None, units.get(key, currency), as_of, provider, status) for key in LABELS}
    if not tail:
        return result
    status = "stale" if as_of != sessions[-1] else "available"

    def put(key, value, unavailable="insufficient"):
        result[key] = Metric(value, units.get(key, currency), as_of, provider, status if value is not None else unavailable)

    closes = [float(bar.close) for bar in tail]
    last = tail[-1]
    put("price", closes[-1])
    put("volume", float(last.volume) if last.volume is not None else None, "missing")
    put("range", float((last.high - last.low) / last.close) * 100)
    for period in (20, 50, 200):
        put(f"sma{period}", _average(closes[-period:]) if len(closes) >= period else None)
    for period in (10, 20):
        put(f"return_{period}", (closes[-1] / closes[-period-1] - 1) * 100 if len(closes) > period else None)
    put("trend", (closes[-1] / result["sma20"].value - 1) * 100 if len(closes) >= 20 else None)
    volumes = [bar.volume for bar in tail[-21:]]
    relative = None
    if len(volumes) == 21 and all(value is not None for value in volumes):
        baseline = sum(volumes[:-1]) / 20
        if baseline > 0:
            relative = float(volumes[-1] / baseline)
    put("relative_volume", relative, "insufficient" if len(volumes) < 21 else "missing")
    put("volatility", pstdev(log(right / left) for left, right in zip(closes[-21:-1], closes[-20:])) * 100
        if len(closes) >= 21 else None)

    changes = [right - left for left, right in zip(closes, closes[1:])]
    rsi = atr = None
    if len(changes) >= 14:
        up = _smooth([max(change, 0) for change in changes], 14, 1 / 14)[-1]
        down = _smooth([max(-change, 0) for change in changes], 14, 1 / 14)[-1]
        rsi = 100 * up / (up + down) if up + down else 50
        true_ranges = [max(float(bar.high), prior) - min(float(bar.low), prior)
                       for bar, prior in zip(tail[1:], closes)]
        atr = _smooth(true_ranges, 14, 1 / 14)[-1]
    put("rsi", rsi)
    put("atr", atr)
    fast, slow = _smooth(closes, 12, 2 / 13), _smooth(closes, 26, 2 / 27)
    macd = [quick - long for quick, long in zip(fast[14:], slow)]
    signal = _smooth(macd, 9, 2 / 10)
    put("macd", macd[-1] if macd else None)
    put("macd_signal", signal[-1] if signal else None)
    put("macd_histogram", macd[-1] - signal[-1] if signal else None)
    start = as_of - timedelta(days=363)
    required = [day for day in sessions if start <= day <= as_of]
    # The caller supplies a calendar covering at least the 52-week boundary.
    annual = [bars[day] for day in required] if sessions[0] <= start and all(day in bars for day in required) else []
    put("high_52", float(max(bar.high for bar in annual)) if annual else None)
    put("low_52", float(min(bar.low for bar in annual)) if annual else None)
    return result
