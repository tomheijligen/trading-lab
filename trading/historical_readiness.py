"""Read-only readiness checks for a daily series valued at venue close in EUR."""
from .historical_data import inspect_coverage
from .historical_fx import inspect_fx_valuations


def inspect_readiness(identity, calendar, plan):
    coverage = inspect_coverage(
        **identity, start_date=plan.start, end_date=plan.end, calendar=calendar,
    )["windows"]["requested"]
    currency = identity["currency"]
    if currency == "EUR":
        fx = {"required": False, "status": "not_required", "covered": True}
    elif currency == "USD":
        # Include warm-up as it can be used for EUR-denominated inputs.
        valuations = {day: calendar.close_at(day) for day in plan.sessions}
        fx = {"required": True, **inspect_fx_valuations(valuations)}
        fx["status"] = "covered" if fx["covered"] else "incomplete"
    else:
        fx = {"required": True, "status": "unsupported_currency", "covered": False}
    ready = coverage["covered"] and fx["covered"]
    return {
        "series": identity,
        "calendar": {"source": calendar.source, "timezone": str(calendar.timezone)},
        "start": plan.start.isoformat(), "end": plan.end.isoformat(),
        "requested_end": plan.requested_end.isoformat(),
        "ohlcv": coverage, "fx": fx,
        "status": "ready" if ready else "incomplete",
    }
