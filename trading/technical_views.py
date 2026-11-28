from django import forms
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .technical_indicators import LABELS
from .technical_screener import screen_rows, technical_rows, technical_watchlist
from .views import page

RANGE_FIELDS = (
    "price", "volume", "relative_volume", "return_10", "return_20", "trend", "volatility",
    "sma20", "sma50", "sma200", "rsi", "macd", "atr",
)
CURRENCY_FIELDS = {"price", "sma20", "sma50", "sma200", "macd", "atr"}


class TechnicalFilters(forms.Form):
    asset_class = forms.ChoiceField(
        choices=(("", "All asset classes"), ("stock", "Stocks"), ("etf", "ETFs"), ("crypto", "Crypto")),
        required=False,
    )
    exchange = forms.ChoiceField(required=False)
    currency = forms.ChoiceField(required=False)
    sort = forms.ChoiceField(choices=[("symbol", "Instrument"), *LABELS.items()], required=False)
    direction = forms.ChoiceField(choices=(("asc", "Ascending"), ("desc", "Descending")), required=False)

    def __init__(self, *args, watchlist, **kwargs):
        super().__init__(*args, **kwargs)
        exchanges = sorted({row["exchange"] for row in watchlist if row["exchange"]})
        currencies = sorted({row["series"]["currency"] for row in watchlist if row["series"]})
        self.fields["exchange"].choices = [("", "All venues"), *((venue, venue) for venue in exchanges)]
        self.fields["currency"].choices = [("", "All currencies"), *((code, code) for code in currencies)]
        for key in RANGE_FIELDS:
            for bound in ("min", "max"):
                self.fields[f"{key}_{bound}"] = forms.DecimalField(
                    required=False, max_digits=24, label=f"{LABELS[key]} {bound}",
                )

    def clean(self):
        data = super().clean()
        for key in RANGE_FIELDS:
            low, high = data.get(f"{key}_min"), data.get(f"{key}_max")
            if low is not None and high is not None and low > high:
                self.add_error(f"{key}_max", "Enter a maximum at least as large as the minimum.")
        if not data.get("currency") and any(
            data.get(f"{key}_{bound}") is not None for key in CURRENCY_FIELDS for bound in ("min", "max")
        ):
            self.add_error("currency", "Choose a currency when filtering by a price-denominated metric.")
        return data


@login_required
@never_cache
@require_GET
def technical_scanner(request):
    if not settings.PLATFORM_LAB_ENABLED:
        raise Http404
    watchlist = technical_watchlist()
    form = TechnicalFilters(request.GET or None, watchlist=watchlist)
    rows = []
    if not form.is_bound or form.is_valid():
        filters = form.cleaned_data if form.is_bound else {}
        selected = [
            row for row in watchlist
            if all(not filters.get(field) or filters[field] == row[field] for field in ("asset_class", "exchange"))
            and (not filters.get("currency") or row["series"] and row["series"]["currency"] == filters["currency"])
        ]
        rows = screen_rows(technical_rows(selected), filters)
    headers = []
    current = request.GET.get("sort", "symbol")
    for key, label in (("symbol", "Instrument"), *LABELS.items()):
        query = request.GET.copy()
        query["sort"] = key
        query["direction"] = "desc" if current == key and request.GET.get("direction") != "desc" else "asc"
        headers.append((label, query.urlencode()))
    return page(
        request, "Technical market screener", technical_screener=True,
        technical_form=form, technical_headers=headers, technical_rows=rows,
        technical_primary=[form[key] for key in ("asset_class", "exchange", "currency", "sort", "direction")],
        technical_ranges=[(form[f"{key}_min"], form[f"{key}_max"]) for key in RANGE_FIELDS],
    )
