from datetime import timedelta

from django import forms
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .models import Simulation
from .strategy_lab import clean_history
from .views import owned, page


class SimulationForm(forms.Form):
    start = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    end = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    capital = forms.DecimalField(min_value=1, max_value=10_000_000, decimal_places=2)
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
        return data


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
        items = list(account.instrument_set.exclude(group="option").order_by("pk")[:26])
        if len(items) > 25:
            form.add_error(None, "Use an account with at most 25 instruments.")
        elif account.simulation_set.filter(status="queued").count() >= 3:
            form.add_error(None, "Wait for a queued simulation to finish.")
        else:
            inputs = {}
            for item in items:
                if item.currency != "EUR":
                    continue
                label = f"{item.symbol} [{item.exchange}]"
                history = clean_history(item.history)
                points = sorted((day, close) for day, close in history.items()
                                if day <= form.cleaned_data["end"])
                warmup = [(day, close) for day, close in points if day < form.cleaned_data["start"]][-30:]
                period = [(day, close) for day, close in points if day >= form.cleaned_data["start"]]
                if len(warmup) == 30 and period:
                    inputs[label] = {day.isoformat(): str(close) for day, close in warmup + period}
            if inputs:
                Simulation.objects.create(account=account, inputs=inputs, **form.cleaned_data)
                return redirect("simulations", pk=pk)
            form.add_error(None, "No EUR instrument has 30 earlier closes and data in this period.")
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
    return JsonResponse({
        "type": "historical_simulation",
        "start": job.start, "end": job.end, "capital_eur": job.capital,
        "fee_bps": job.fee_bps, "spread_bps": job.spread_bps, "slippage_bps": job.slippage_bps,
        "instruments": list(job.inputs), "results": job.result,
    }, headers={"Content-Disposition": f'attachment; filename="simulation-{job.pk}.json"'})
