import re
from uuid import uuid4

from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User

from .models import Account, Deposit, Instrument, ZERO


class RegisterForm(UserCreationForm):
    email = forms.EmailField(max_length=150)

    class Meta:
        model = User
        fields = ("email",)

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()

    def save(self, commit=True):
        user = super().save(commit=False)
        # A public registration always starts as an inactive regular user.
        user.username = user.email = self.cleaned_data["email"]
        user.is_active = False
        if commit:
            user.save()
        return user


class AccountForm(forms.ModelForm):
    starting_cash = forms.DecimalField(
        label="Starting capital (€)", min_value=0, max_value=10_000_000,
        decimal_places=2, required=False,
    )

    class Meta:
        model = Account
        fields = ("name", "mode", "bot", "starting_cash", "stock", "etf", "option", "crypto", "automatic")
        labels = {
            "stock": "Stocks (%)", "etf": "ETFs (%)", "option": "Options (%)",
            "crypto": "Crypto (%)", "automatic": "Include in scheduled bot rounds",
        }

    def clean(self):
        values = super().clean()
        for field in ("stock", "etf", "option", "crypto"):
            if values.get(field, 0) > 100:
                self.add_error(field, "Enter a percentage from 0 to 100.")
        if sum(values.get(field, 0) for field in ("stock", "etf", "option", "crypto")) > 100:
            self.add_error(None, "The targets must add up to 100% or less.")
        if values.get("mode") == "real":
            values.update(starting_cash=ZERO, automatic=False)
        else:
            values["starting_cash"] = values.get("starting_cash") or ZERO
            if values["starting_cash"] <= ZERO:
                self.add_error("starting_cash", "Enter positive capital for virtual trading.")
        return values


class StopForm(forms.ModelForm):
    stop = forms.IntegerField(label="Fixed stop (%) · 0 disables", min_value=0, max_value=99, initial=0)
    trailing = forms.IntegerField(label="Trailing stop (%) · 0 disables", min_value=0, max_value=99, initial=0)

    class Meta:
        model = Instrument
        fields = ("stop", "trailing")


class InstrumentForm(StopForm):
    class Meta:
        model = Instrument
        fields = ("symbol", "exchange", "group", "stop", "trailing")

    def clean(self):
        values = super().clean()
        symbol = values.get("symbol", "").strip().upper()
        exchange = values.get("exchange", "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9./_-]{1,35}", symbol):
            self.add_error("symbol", "Enter a valid ticker or option contract symbol.")
        if values.get("group") == "crypto" and not re.fullmatch(r"[A-Z0-9]+/[A-Z]{3}", symbol):
            self.add_error("symbol", "Use a currency pair such as BTC/EUR.")
        values.update(symbol=symbol, exchange=exchange)
        return values


class DepositForm(forms.ModelForm):
    reference = forms.UUIDField(initial=uuid4, widget=forms.HiddenInput)
    amount = forms.DecimalField(label="Amount (€)", min_value=0.01, max_value=10_000_000, decimal_places=2)

    class Meta:
        model = Deposit
        fields = ("reference", "amount", "destination")


class OptionHistoryForm(forms.Form):
    quotations = forms.CharField(
        label="Paste CSV with date,close_eur headers",
        max_length=30_000, widget=forms.Textarea(attrs={"rows": 8}),
    )