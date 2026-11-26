from io import StringIO
from unittest import skipUnless
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings

from config.database_guard import check_test_target, target


class DatabaseGuardTests(SimpleTestCase):
    @override_settings(TRADING_ISOLATED_TESTS=False)
    def test_standard_test_command_is_rejected_before_connecting(self):
        with patch("config.database_guard.confirm_postgres") as confirm:
            with self.assertRaisesMessage(CommandError, "Operational databases are forbidden"):
                check_test_target(connection)
        confirm.assert_not_called()

    @override_settings(TRADING_ISOLATED_TESTS=False)
    def test_missing_and_wrong_migration_targets_are_rejected_before_connecting(self):
        with patch("trading.management.commands.migrate.confirm_postgres") as confirm:
            for value in ("", "127.0.0.1:5432/trading_lab_v2"):
                with patch.dict("os.environ", {"TRADING_MIGRATION_TARGET": value}):
                    with self.assertRaisesMessage(CommandError, "intended configured host:port/database"):
                        call_command("migrate", plan=True)
        confirm.assert_not_called()


@skipUnless(connection.vendor == "postgresql", "Requires isolated PostgreSQL")
class PostgreSQLMigrationGuardTests(TestCase):
    @override_settings(TRADING_ISOLATED_TESTS=False)
    def test_matching_migration_target_is_verified_on_the_real_test_connection(self):
        output = StringIO()
        with patch.dict("os.environ", {"TRADING_MIGRATION_TARGET": target(connection)}):
            call_command("migrate", plan=True, stdout=output)
        self.assertIn("Verified migration target:", output.getvalue())
        self.assertIn("No planned migration operations.", output.getvalue())
