from django.core.management.base import BaseCommand

from trading.engine import DataError, run_account
from trading.models import Account


class Command(BaseCommand):
    help = "Run one simulated round for each enabled virtual account."

    def handle(self, *args, **options):
        accounts = (
            Account.objects.filter(
                mode="virtual",
                automatic=True,
                owner__is_active=True,
            )
            .order_by("pk")
            .values_list("pk", flat=True)
        )

        # Share market snapshots between accounts for this round only.
        market_cache = {}

        for account_id in accounts.iterator():
            try:
                result = run_account(
                    account_id,
                    execute=True,
                    scheduled=True,
                    market_cache=market_cache,
                )
                self.stdout.write(f"Account {account_id}: {result}")
            except DataError as error:
                self.stderr.write(f"Account {account_id}: {error}")