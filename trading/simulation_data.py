"""Read validated EUR market data for immutable simulation inputs."""
from .historical_backfill import make_plan
from .historical_calendars import load_calendar
from .historical_providers import instrument_identity
from .historical_readiness import inspect_readiness
from .models import HistoricalBar

OHLCV_SCHEMA = "historical_ohlcv_v1"
OHLCV_FILL_MODEL = "session_open_after_available_close"


def historical_simulation_inputs(provider, asset_class, symbol, exchange, currency, adjustment, *, start, end):
    identity = instrument_identity(provider, asset_class, symbol, exchange, currency, adjustment)
    if identity["currency"] != "EUR":
        raise ValueError("Historical OHLCV simulations currently require source prices in EUR.")
    calendar = load_calendar(asset_class, identity["exchange"])
    plan = make_plan(calendar, start=start, end=end)
    if plan.end != end:
        raise ValueError("Historical OHLCV simulations require completed venue dates.")
    readiness = inspect_readiness(identity, calendar, plan)
    if readiness["status"] != "ready":
        coverage = readiness["ohlcv"]
        raise ValueError(
            f"Historical data is incomplete: {coverage['available_bars']}/{coverage['expected_bars']} "
            f"usable sessions, {len(coverage['gap_dates'])} missing, "
            f"{len(coverage['rejected_dates'])} rejected, {coverage['warmup_bars']}/30 warm-up sessions."
        )
    rows = list(HistoricalBar.objects.filter(
        **identity, session_date__in=plan.sessions, quality=HistoricalBar.Quality.VALID,
    ).order_by("session_date"))
    if len(rows) != len(plan.sessions):
        raise ValueError("Historical data changed while preparing the simulation. Try again.")
    bars = {}
    for bar in rows:
        day = bar.session_date
        opening, close = calendar.open_at(day), calendar.close_at(day)
        if not opening < close or bar.observed_at > close:
            raise ValueError(f"Historical OHLCV timing is not usable at session close on {day}.")
        bars[day.isoformat()] = {
            "open": str(bar.open), "high": str(bar.high), "low": str(bar.low),
            "close": str(bar.close), "volume": str(bar.volume) if bar.volume is not None else None,
            "opens_at": opening.isoformat(), "available_at": close.isoformat(),
        }
    return {
        "schema": OHLCV_SCHEMA, "series": identity, "bars": bars,
        "calendar": readiness["calendar"], "fill_model": OHLCV_FILL_MODEL,
    }
