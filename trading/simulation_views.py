from datetime import timedelta

from django import forms
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .historical_providers import PROVIDERS, TWELVE_ADJUSTMENTS
from .models import Simulation
from .simulation_data import OHLCV_SCHEMA, historical_simulation_inputs
from .paper_metrics import matching_paper_reference
from .strategy_lab import clean_history
from .views import owned, page


SERIES_FIELDS = ("provider", "asset_class", "symbol", "exchange", "currency", "adjustment")


class SimulationForm(forms.Form):
    source = forms.ChoiceField(
        choices=(("account", "Account close history"), ("ohlcv", "Validated EUR OHLCV")),
        required=False,
    )
    provider = forms.ChoiceField(choices=[("", "---------")] + [
        (key, provider.name) for key, provider in PROVIDERS.items()
    ], required=False)
    asset_class = forms.ChoiceField(choices=(
        ("", "---------"), ("stock", "Stock"), ("etf", "ETF"), ("crypto", "Crypto"),
    ), required=False)
    symbol = forms.CharField(max_length=35, required=False)
    exchange = forms.CharField(max_length=35, required=False, label="Exchange MIC or crypto venue")
    currency = forms.ChoiceField(choices=(("", "---------"), ("EUR", "EUR")), required=False)
    adjustment = forms.ChoiceField(
        choices=[("", "---------")] + [(key, key) for key in TWELVE_ADJUSTMENTS], required=False,
    )

    start = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    end = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    capital = forms.DecimalField(required=False, min_value=1, max_value=10_000_000, decimal_places=2)
    match_paper = forms.BooleanField(
        required=False, label="Use a recorded paper period and its starting portfolio value",
    )
    fee_bps = forms.IntegerField(min_value=0, max_value=500, initial=20)
    spread_bps = forms.IntegerField(min_value=0, max_value=500, initial=10)
    slippage_bps = forms.IntegerField(min_value=0, max_value=500, initial=5)

    def clean(self):
        data = super().clean()
        if "start" in data and "end" in data:
            if not data["start"] <= data["end"] < timezone.localdate():
                raise forms.ValidationError("Select an ordered period ending before today.")
            if data["end"] - data["start"] > timedelta(days=730):
                raise forms.ValidationError("Choose a period of at most 731 days.")
        if not data.get("match_paper") and not data.get("capital"):
            self.add_error("capital", "Enter capital for a standalone historical simulation.")
        if data.get("source") == "ohlcv":
            for field in SERIES_FIELDS:
                if not data.get(field) and not self.has_error(field):
                    self.add_error(field, "Required for a validated OHLCV simulation.")
        return data


def account_simulation_inputs(account, start, end):
    items = list(account.instrument_set.exclude(group="option").order_by("pk")[:26])
    if len(items) > 25:
        raise ValueError("Use an account with at most 25 instruments.")
    inputs = {}
    for item in items:
        if item.currency != "EUR":
            continue
        label = f"{item.symbol} [{item.exchange}]"
        history = clean_history(item.history)
        points = sorted((day, close) for day, close in history.items() if day <= end)
        warmup = [(day, close) for day, close in points if day < start][-30:]
        period = [(day, close) for day, close in points if day >= start]
        if len(warmup) == 30 and period:
            inputs[label] = {day.isoformat(): str(close) for day, close in warmup + period}
    if not inputs:
        raise ValueError("No EUR instrument has 30 earlier closes and data in this period.")
    return inputs


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def simulations(request, pk):
    account = owned(request, pk)
    if not settings.PLATFORM_LAB_ENABLED or account.mode != "virtual":
        raise Http404
    today = timezone.localdate()
    form = SimulationForm(
        request.POST if request.method == "POST" else None,
        initial={"capital": account.starting_cash, "start": today - timedelta(days=30),
                 "end": today - timedelta(days=1)},
    )
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data.copy()
        source = data.pop("source")
        series = {field: data.pop(field) for field in SERIES_FIELDS}
        try:
            if source == "ohlcv":
                inputs = historical_simulation_inputs(**series, start=data["start"], end=data["end"])
            else:
                inputs = account_simulation_inputs(account, data["start"], data["end"])
        except ValueError as error:
            form.add_error(None, str(error))
        if not form.errors:
            with transaction.atomic():
                account = owned(request, pk, locked=True)
                reference = {}
                if account.simulation_set.filter(status="queued").count() >= 3:
                    form.add_error(None, "Wait for a queued simulation to finish.")
                elif data.pop("match_paper"):
                    reference = matching_paper_reference(account, data["start"], data["end"])
                    if reference:
                        data["capital"] = reference["capital"]
                    else:
                        form.add_error(
                            None, "A matching paper comparison needs valid boundary marks and no deposits or switches.",
                        )
                if not form.errors:
                    Simulation.objects.create(
                        account=account, inputs=inputs, paper_reference=reference, **data,
                    )
                    return redirect("simulations", pk=pk)
    return page(
        request, "Historical simulations", account=account, form=form,
        submit="Queue strategy comparison", simulation_jobs=account.simulation_set.order_by("-pk")[:10],
        historical_simulations=True,
    )


@login_required
@never_cache
@require_http_methods(["GET"])
def simulation_export(request, pk, job_id):
    account = owned(request, pk)
    if not settings.PLATFORM_LAB_ENABLED:
        raise Http404
    job = get_object_or_404(Simulation, pk=job_id, account=account, status="done")
    historical = job.inputs.get("schema") == OHLCV_SCHEMA
    instruments = [job.inputs["series"]["symbol"]] if historical else list(job.inputs)
    return JsonResponse({
        "type": "historical_simulation",
        "start": job.start, "end": job.end, "capital_eur": job.capital,
        "fee_bps": job.fee_bps, "spread_bps": job.spread_bps, "slippage_bps": job.slippage_bps,
        "instruments": instruments, "paper_reference": job.paper_reference, "results": job.result,
    }, headers={"Content-Disposition": f'attachment; filename="simulation-{job.pk}.json"'})
