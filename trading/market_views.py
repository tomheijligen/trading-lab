import os

from django import forms
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .market_data import ALPACA, BINANCE, TWELVE, alpaca_headers
from .models import ProviderUsage
from .scanner import rank_observations, scanner_universe
from .views import page


class RankingForm(forms.Form):
    momentum = forms.IntegerField(min_value=0, max_value=100, initial=1)
    trend = forms.IntegerField(min_value=0, max_value=100, initial=1)
    volatility = forms.IntegerField(min_value=0, max_value=100, initial=1)


@login_required
@never_cache
@require_GET
def scanner(request):
    if not settings.PLATFORM_LAB_ENABLED:
        raise Http404
    form = RankingForm(request.GET or None)
    weights = (1, 1, 1)
    if form.is_valid():
        weights = tuple(form.cleaned_data[field] for field in form.fields)
    return page(request, "Market scanner", market_scanner=True, ranking_form=form,
                ranked=rank_observations(scanner_universe(), weights))


@login_required
@never_cache
@require_GET
def providers(request):
    if not settings.PLATFORM_LAB_ENABLED or not request.user.is_staff:
        raise Http404
    now = timezone.now()
    providers = []
    for provider, enabled in (
        (TWELVE, bool(os.getenv("TWELVE_DATA_API_KEY"))),
        (BINANCE, settings.MARKET_BINANCE_ENABLED),
        (ALPACA, bool(alpaca_headers())),
    ):
        usage = ProviderUsage.objects.filter(pk=provider.name).first()
        providers.append({
            "name": provider.name, "enabled": enabled, "usage": usage,
            "minute": usage.minute_credits if usage and usage.minute == now.replace(second=0, microsecond=0) else 0,
            "day": usage.day_credits if usage and usage.day == now.date() else 0,
            "minute_limit": provider.minute_limit, "day_limit": provider.daily_limit,
            "blocked": usage.blocked_until if usage and usage.blocked_until and usage.blocked_until > now else None,
        })
    return page(request, "Market data providers", market_providers=providers)
