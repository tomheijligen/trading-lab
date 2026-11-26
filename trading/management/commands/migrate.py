import os

from django.conf import settings
from django.core.management.base import CommandError, OutputWrapper
from django.core.management.commands.migrate import Command as DjangoMigrate
from django.db import connections

from config.database_guard import check_test_target, confirm_postgres, target


class Command(DjangoMigrate):
    def execute(self, *args, **options):
        connection = connections[options["database"]]
        if getattr(settings, "TRADING_ISOLATED_TESTS", False):
            check_test_target(connection, migrate=True)
        else:
            expected = os.getenv("TRADING_MIGRATION_TARGET")
            if connection.vendor != "postgresql" or not expected or expected != target(connection):
                raise CommandError("Set TRADING_MIGRATION_TARGET to the intended configured host:port/database.")
            confirm_postgres(connection)
            output = OutputWrapper(options["stdout"]) if options.get("stdout") else self.stdout
            output.write(f"Verified migration target: {target(connection)}")
        return super().execute(*args, **options)
