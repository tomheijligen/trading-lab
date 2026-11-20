import calendar
import csv
import io
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .engine import DataError, amount, run_account, signal_for
from .forms import (
    AccountForm,
    DepositForm,
    InstrumentForm,
    OptionHistoryForm,
    RegisterForm,
    StopForm,
)
from .models import (
    GROUPS,
    ZERO,
    Account,
    Deposit,
    Instrument,
    PendingActivation,
    ReportSubscription,
    Trade,
)
from .paper_metrics import paper_windows, strategy_periods
from .performance import order_history, performance_charts


PERIODS = [
    ("day", "Daily"),
    ("week", "Weekly"),
    ("month", "Monthly"),
    ("quarter", "Quarterly"),
    ("half", "Six months"),
    ("year", "Yearly"),
]


def page(request, title, **context):
    return render(
        request,
        "trading/page.html",
        {"title": title, **context},
    )


def owned(request, pk, *, locked=False):
    # Every account lookup includes the signed-in owner.
    accounts = Account.objects.select_for_update() if locked else Account.objects.all()
    return get_object_or_404(accounts, pk=pk, owner=request.user)


@never_cache
@require_http_methods(["GET", "POST"])
def register(request):
    if not settings.REGISTRATION_OPEN:
        raise Http404

    form = RegisterForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                user = form.save()
                pending = PendingActivation.objects.create(user=user)
        except IntegrityError:
            messages.info(request, "If this email can be registered, check its inbox.")
            return redirect("login")

        link = settings.PUBLIC_ORIGIN + reverse("activate", args=[pending.token])
        try:
            send_mail(
                "Activate your Trading Lab account",
                f"Open this link within 24 hours and confirm your email:\n{link}",
                settings.DEFAULT_FROM_EMAIL,
                [user.email],
            )
        except OSError:
            form.add_error(None, "Email delivery failed. Contact the administrator.")
        else:
            messages.info(request, "Check your email and confirm your account.")
            return redirect("login")

    return page(request, "Register", form=form, submit="Create login")


@never_cache
@require_http_methods(["GET", "POST"])
def activate(request, token):
    cutoff = timezone.now() - timedelta(days=1)

    if request.method == "GET":
        get_object_or_404(
            PendingActivation,
            token=token,
            created__gte=cutoff,
        )
        return page(request, "Confirm your email", confirmation=True)

    # Consume the activation record so the confirmation link is single-use.
    with transaction.atomic():
        pending = get_object_or_404(
            PendingActivation.objects.select_for_update(),
            token=token,
            created__gte=cutoff,
        )
        user = pending.user
        if user.is_active or user.is_staff:
            raise Http404
        user.is_active = True
        user.save(update_fields=["is_active"])
        pending.delete()

    messages.success(request, "Your email is confirmed. You can sign in.")
    return redirect("login")


@login_required
@never_cache
@require_GET
def dashboard(request):
    accounts = Account.objects.filter(owner=request.user).order_by("mode", "name", "pk")
    return page(
        request,
        "Your trading accounts",
        accounts=accounts,
        dashboard=True,
        market_research=settings.PLATFORM_LAB_ENABLED,
        charts=performance_charts(request.user),
        chart_scope="All virtual accounts",
    )


@login_required
@require_http_methods(["GET", "POST"])
def create_account(request):
    form = AccountForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        account = form.save(commit=False)
        account.owner = request.user
        account.cash = account.starting_cash
        account.save()
        return redirect("account", pk=account.pk)

    return page(
        request,
        "Create a trading account",
        form=form,
        submit="Create account",
    )


@login_required
@never_cache
@require_GET
def account_page(request, pk):
    account = owned(request, pk)
    instruments = list(account.instrument_set.order_by("group", "symbol", "pk"))
    is_virtual = account.mode == "virtual"

    # Show only instruments with units currently held in this account.
    positions = [
        {
            "instrument": item,
            "value": amount(item.value),
            "profit": amount(item.value - item.cost),
        }
        for item in instruments
        if item.units > ZERO
    ]

    value = amount(
        account.cash + account.reserve + sum((item.value for item in instruments), ZERO)
    )
    deposits = account.deposit_set.order_by("-created", "-pk")
    booked = deposits.filter(booked=True).aggregate(total=Sum("amount"))["total"] or ZERO

    groups = [
        (
            label,
            getattr(account, group),
            amount(sum((item.value for item in instruments if item.group == group), ZERO)),
        )
        for group, label in GROUPS
    ]

    return page(
        request,
        account.name,
        account=account,
        overview=True,
        groups=groups,
        value=value,
        profit=value - booked - account.starting_cash,
        deposits=deposits[:30],
        deposit_form=DepositForm(),
        show_positions=is_virtual,
        platform_lab=is_virtual and settings.PLATFORM_LAB_ENABLED,
        paper_windows=paper_windows(account) if is_virtual and settings.PLATFORM_LAB_ENABLED else [],
        strategy_periods=strategy_periods(account) if is_virtual and settings.PLATFORM_LAB_ENABLED else [],
        positions=positions,
        charts=performance_charts(request.user, account.pk) if is_virtual else [],
        chart_scope=account.name,
        trade_page=order_history(request, account.pk) if is_virtual else None,
    )


@login_required
@require_POST
def add_deposit(request, pk):
    with transaction.atomic():
        account = owned(request, pk, locked=True)
        form = DepositForm(request.POST)
        if not form.is_valid():
            for errors in form.errors.values():
                for error in errors:
                    messages.error(request, error)
            return redirect("account", pk=pk)

        data = form.cleaned_data
        deposit, created = Deposit.objects.get_or_create(
            account=account,
            reference=data["reference"],
            defaults={
                "amount": data["amount"],
                "destination": data["destination"],
                "booked": account.mode == "virtual",
            },
        )

        # Repeating the same submission must not credit the balance twice.
        if not created and (
            deposit.amount != data["amount"]
            or deposit.destination != data["destination"]
        ):
            return HttpResponse("This deposit reference has already been used.", status=409)

        if created and deposit.booked:
            field = deposit.destination
            setattr(account, field, getattr(account, field) + deposit.amount)
            account.save(update_fields=[field])

    return redirect("account", pk=pk)


@login_required
@require_http_methods(["GET", "POST"])
def add_instrument(request, pk):
    account = owned(request, pk)
    if account.mode != "virtual":
        raise Http404

    form = InstrumentForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                account = owned(request, pk, locked=True)
                if account.instrument_set.filter(auto_discovered=False).count() >= 10:
                    form.add_error(None, "You can add up to ten instruments manually.")
                else:
                    instrument = form.save(commit=False)
                    instrument.account = account
                    instrument.save()
                    return redirect("market", pk=pk)
        except IntegrityError:
            form.add_error(None, "This symbol and exchange are already in the account.")

    return page(
        request,
        "Add a market instrument",
        account=account,
        form=form,
        submit="Add instrument",
    )


@login_required
@require_http_methods(["GET", "POST"])
def stops(request, pk, item):
    account = owned(request, pk)
    instrument = get_object_or_404(Instrument, account=account, pk=item)
    form = StopForm(
        request.POST if request.method == "POST" else None,
        instance=instrument,
    )

    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            account = owned(request, pk, locked=True)
            updated = Instrument.objects.filter(account=account, pk=item).update(
                **form.cleaned_data
            )
            if not updated:
                raise Http404
        return redirect("market", pk=pk)

    return page(
        request,
        f"Stops for {instrument.symbol}",
        account=account,
        form=form,
        submit="Save stops",
    )


@login_required
@require_POST
def toggle_schedule(request, pk):
    with transaction.atomic():
        account = owned(request, pk, locked=True)
        if account.mode != "virtual":
            raise Http404

        # Discovery and scheduled execution are independent settings.
        if "discovery" in request.POST:
            account.auto_discovery = request.POST["discovery"] == "1"
            account.save(update_fields=["auto_discovery"])
        else:
            account.automatic = request.POST.get("enabled") == "1"
            account.save(update_fields=["automatic"])

    return redirect("account", pk=pk)


@login_required
@require_POST
def run(request, pk):
    account = owned(request, pk)
    if account.mode != "virtual":
        raise Http404

    # Refresh requests fetch quotes only. Trade requests may place virtual orders.
    execute = request.POST.get("action") == "trade"

    try:
        result = run_account(account.pk, execute=execute)
        messages.info(request, result)
    except Account.DoesNotExist:
        # The account may have been deleted while quotes were being fetched.
        raise Http404 from None
    except DataError as error:
        messages.warning(request, str(error))

    if execute:
        return redirect("account", pk=pk)
    return redirect("market", pk=pk)


@login_required
@never_cache
@require_GET
def market(request, pk):
    account = owned(request, pk)
    if account.mode != "virtual":
        raise Http404

    return page(
        request,
        "Market history and outlook",
        account=account,
        market=True,
        instruments=account.instrument_set.order_by("group", "symbol", "pk"),
    )


@login_required
@require_http_methods(["GET", "POST"])
def option_history(request, pk, item):
    account = owned(request, pk)
    instrument = get_object_or_404(Instrument, account=account, pk=item, group="option")
    form = OptionHistoryForm(request.POST if request.method == "POST" else None)

    if request.method == "POST" and form.is_valid():
        try:
            reader = csv.DictReader(io.StringIO(form.cleaned_data["quotations"]))
            if reader.fieldnames != ["date", "close_eur"]:
                raise ValueError("Use exactly date,close_eur as the header.")

            rows = {}
            for row in reader:
                day = date.fromisoformat(row["date"])
                premium = Decimal(row["close_eur"])
                if (
                    day in rows
                    or day > timezone.localdate()
                    or not premium.is_finite()
                    or not ZERO <= premium <= Decimal("1000000")
                    or len(rows) >= 500
                ):
                    raise ValueError("Use up to 500 unique dates and valid premiums.")
                rows[day] = premium.quantize(Decimal("0.00000001"))

            if not rows:
                raise ValueError("Add at least one price row.")

            dates = sorted(rows)
            history = [[day.isoformat(), str(rows[day])] for day in dates]
            closes = [rows[day] for day in dates]
            if all(closes):
                signal, explanation = signal_for(account.bot, closes)
            else:
                signal = "hold"
                explanation = "A zero premium appears in this history. No signal is calculated."
        except (TypeError, ValueError, InvalidOperation, csv.Error) as error:
            form.add_error("quotations", f"Invalid CSV. {error}")
        else:
            with transaction.atomic():
                account = owned(request, pk, locked=True)
                updated = Instrument.objects.filter(
                    account=account,
                    pk=item,
                    group="option",
                ).update(
                    history=history,
                    source="Imported CSV",
                    currency="EUR",
                    signal=signal,
                    explanation=explanation,
                )
                if not updated:
                    raise Http404
            return redirect("market", pk=pk)

    return page(
        request,
        f"Import history for {instrument.symbol}",
        account=account,
        form=form,
        submit="Import history",
    )


def period_start(now, key):
    # Longer ranges use calendar months, with the day clamped to the target month.
    if key in {"day", "week"}:
        return now - timedelta(days=1 if key == "day" else 7)

    months = {"month": 1, "quarter": 3, "half": 6, "year": 12}[key]
    year, month_index = divmod(now.year * 12 + now.month - 1 - months, 12)
    month = month_index + 1
    day = min(now.day, calendar.monthrange(year, month)[1])
    return now.replace(year=year, month=month, day=day)


@login_required
@never_cache
@require_GET
def reports(request, pk):
    account = owned(request, pk)
    if account.mode != "virtual":
        raise Http404

    selected = request.GET.get("period", "month")
    if selected not in dict(PERIODS):
        raise Http404

    end = timezone.now()
    start = period_start(end, selected)
    trades = (
        Trade.objects.filter(
            instrument__account=account,
            created__gte=start,
            created__lte=end,
        )
        .select_related("instrument")
        .order_by("-created", "-pk")
    )
    exits = trades.filter(side="Sell")
    email_periods = list(account.report_subscriptions.values_list("period", flat=True))

    return page(
        request,
        "Trade reports",
        account=account,
        reports=True,
        periods=PERIODS,
        selected=selected,
        start=start,
        end=end,
        count=trades.count(),
        trades=trades[:200],
        wins=exits.filter(profit__gt=0).count(),
        losses=exits.filter(profit__lt=0).count(),
        flat=exits.filter(profit=0).count(),
        net=exits.aggregate(total=Sum("profit"))["total"] or ZERO,
        email_periods=email_periods,
    )


@login_required
@require_POST
def report_email_settings(request, pk):
    selected = set(request.POST.getlist("email_periods"))
    if not selected <= set(ReportSubscription.Period.values):
        return HttpResponse("Invalid report period.", status=400)

    with transaction.atomic():
        account = owned(request, pk, locked=True)
        if account.mode != "virtual":
            raise Http404
        if selected and not request.user.email.strip():
            messages.error(request, "Add an email address to your login first.")
            return redirect("reports", pk=pk)

        account.report_subscriptions.exclude(period__in=selected).delete()
        for period in sorted(selected):
            account.report_subscriptions.get_or_create(period=period)

    messages.success(request, "Email report settings saved.")
    return redirect("reports", pk=pk)


@login_required
@never_cache
@require_GET
def all_signals(request):
    # Read stored signals without making any market API requests.
    accounts = (
        Account.objects.filter(owner=request.user, mode="virtual")
        .prefetch_related("instrument_set")
        .order_by("name", "pk")
    )
    cutoff = timezone.now() - timedelta(minutes=30)
    signal_groups = []

    for account in accounts:
        instruments = sorted(
            account.instrument_set.all(),
            key=lambda item: (item.group, item.symbol, item.exchange),
        )
        signal_groups.append({
            "account": account,
            "rows": [
                {
                    "instrument": item,
                    "quote_is_old": bool(item.quote_time and item.quote_time < cutoff),
                }
                for item in instruments
            ],
        })

    return page(request, "All account signals", signal_groups=signal_groups)


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def delete_account(request, pk):
    if request.method == "GET":
        account = owned(request, pk)
        return page(
            request,
            "Delete trading account",
            account=account,
            confirm_account_deletion=True,
        )

    # Only a confirmed POST can delete an account owned by this user.
    with transaction.atomic():
        account = owned(request, pk, locked=True)
        name = account.name
        account.delete()

    messages.success(request, f"Trading account deleted: {name}.")
    return redirect("dashboard")