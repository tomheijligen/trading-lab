import calendar
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from trading.models import Account, ReportSubscription, Trade, ZERO


def next_delivery(anchor, period):
    """Add a calendar interval and handle shorter months."""
    if period == "day":
        return anchor + timedelta(days=1)
    if period == "week":
        return anchor + timedelta(days=7)

    months = {"month": 1, "quarter": 3, "half": 6, "year": 12}[period]
    year, month = divmod(anchor.year * 12 + anchor.month - 1 + months, 12)
    month += 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return anchor.replace(year=year, month=month, day=day)


def report_body(account, subscription, start, end):
    # Count all orders since the previous successful delivery.
    orders = Trade.objects.filter(
        instrument__account=account, created__gt=start, created__lte=end
    ).select_related("instrument").order_by("-created", "-pk")
    exits = orders.filter(side="Sell")
    net = exits.aggregate(total=Sum("profit"))["total"] or ZERO

    lines = [
        f"{subscription.get_period_display()} report",
        f"Account: {account.name}",
        f"Period: {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} UTC",
        "",
        f"Orders: {orders.count()}",
        f"Winning exits: {exits.filter(profit__gt=0).count()}",
        f"Losing exits: {exits.filter(profit__lt=0).count()}",
        f"Break-even exits: {exits.filter(profit=0).count()}",
        f"Realized result after fees: EUR {net:.2f}",
        "",
        "Latest order explanations (up to 20):",
    ]

    recent = list(orders[:20])
    if not recent:
        lines.append("No orders were recorded in this period.")
    for order in recent:
        outcome = (
            f" | Realized result: EUR {order.profit:.2f}"
            if order.profit is not None else ""
        )
        lines.append(
            f"- {order.created:%Y-%m-%d %H:%M} UTC | "
            f"{order.side} {order.instrument.symbol} | "
            f"{order.get_bot_display()} | {order.reason}{outcome}"
        )

    lines.extend([
        "",
        f"Open Reports or change your email settings: "
        f"{settings.PUBLIC_ORIGIN}/accounts/{account.pk}/reports/",
    ])
    return "\n".join(lines)


class Command(BaseCommand):
    help = "Send due email reports for active virtual accounts."

    def add_arguments(self, parser):
        parser.add_argument(
            "--preview",
            type=int,
            metavar="SUBSCRIPTION_ID",
            help="Print a report locally without sending it or changing its schedule.",
        )

    def handle(self, *args, **options):
        end = timezone.now()
        eligible = ReportSubscription.objects.filter(
            account__mode="virtual", account__owner__is_active=True,
        ).exclude(account__owner__email="")

        preview = options["preview"]
        if preview is not None:
            if not settings.LOCAL:
                self.stderr.write("Preview is available only in local development.")
                return
            subscription = eligible.select_related("account").filter(pk=preview).first()
            if subscription is None:
                self.stderr.write("Subscription not found.")
                return
            start = subscription.last_sent or subscription.created
            self.stdout.write(report_body(subscription.account, subscription, start, end))
            return

        candidates = list(eligible.values_list("pk", "account_id"))
        sent = 0
        for subscription_id, account_id in candidates:
            try:
                with transaction.atomic():
                    # Lock in the same order as the settings view.
                    account = Account.objects.select_for_update().filter(pk=account_id).first()
                    subscription = eligible.select_for_update(of=("self",)).filter(
                        pk=subscription_id
                    ).first()
                    if account is None or subscription is None:
                        continue

                    start = subscription.last_sent or subscription.created
                    if end < next_delivery(start, subscription.period):
                        continue

                    delivered = send_mail(
                        "Trading Lab - scheduled virtual trading report",
                        report_body(account, subscription, start, end),
                        settings.DEFAULT_FROM_EMAIL,
                        [account.owner.email],
                        fail_silently=False,
                    )
                    if delivered != 1:
                        self.stderr.write(f"Delivery failed for subscription {subscription_id}.")
                        continue

                    # Leave the previous date in place when delivery fails so the task can retry.
                    subscription.last_sent = end
                    subscription.save(update_fields=["last_sent"])
                    sent += 1
            except OSError:
                self.stderr.write(f"Email service failed for subscription {subscription_id}.")

        self.stdout.write(f"{sent} report(s) sent.")