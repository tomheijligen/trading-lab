from django import forms
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .bot_control import TREATMENTS, switch_bot
from .engine import DataError
from .models import BOTS
from .views import owned, page


class SwitchForm(forms.Form):
    revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    new_bot = forms.ChoiceField(choices=BOTS)
    treatment = forms.ChoiceField(choices=TREATMENTS)


class RiskForm(forms.Form):
    max_position_pct = forms.IntegerField(min_value=1, max_value=100, label="Maximum position (%)")
    max_exposure_pct = forms.IntegerField(min_value=1, max_value=100, label="Maximum exposure (%)")
    max_loss_pct = forms.IntegerField(min_value=0, max_value=100, label="Loss limit since funding (%), 0 disables")


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def bot_settings(request, pk, action):
    account = owned(request, pk)
    if not settings.PLATFORM_LAB_ENABLED or account.mode != "virtual":
        raise Http404
    switching = action == "switch"
    data = request.POST if request.method == "POST" else None
    if switching:
        form = SwitchForm(data, initial={"revision": account.bot_revision})
        form.fields["new_bot"].choices = [(key, label) for key, label in BOTS if key != account.bot]
    else:
        form = RiskForm(data, initial={name: getattr(account, name) for name in RiskForm.base_fields})
    if request.method == "POST" and form.is_valid():
        if switching:
            try:
                switch_bot(account_id=pk, owner=request.user, **form.cleaned_data)
            except DataError as error:
                form.add_error(None, str(error))
            else:
                return redirect("account", pk=pk)
        else:
            with transaction.atomic():
                account = owned(request, pk, locked=True)
                for name, value in form.cleaned_data.items():
                    setattr(account, name, value)
                account.save(update_fields=list(form.cleaned_data))
            return redirect("account", pk=pk)
    return page(
        request, "Switch strategy" if switching else "Paper trading risk limits",
        account=account, form=form, submit="Save",
        bot_switches=account.botswitch_set.order_by("-created", "-pk")[:30] if switching else None,
    )
