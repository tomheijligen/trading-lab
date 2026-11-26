"""Preflight checks for migration and test commands."""
from django.conf import settings
from django.core.management.base import CommandError

PROTECTED_DB = "trading_lab_v2"


def target(connection):
    config = connection.settings_dict
    return f"{config['HOST']}:{config['PORT']}/{config['NAME']}"


def confirm_postgres(connection):
    config = connection.settings_dict
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database(), current_user, inet_server_port()")
        actual = cursor.fetchone()
    if actual != (config["NAME"], config["USER"], int(config["PORT"])):
        raise CommandError("Connected PostgreSQL database, role or server port differs from the approved target.")


def check_test_target(connection, *, migrate=False):
    if not getattr(settings, "TRADING_ISOLATED_TESTS", False):
        raise CommandError("Use explicit Trading Lab test settings. Operational databases are forbidden for tests.")
    config = connection.settings_dict
    if connection.vendor == "sqlite" and config["NAME"] == ":memory:":
        return
    test_name = config["TEST"].get("NAME") or ""
    if (
        connection.vendor != "postgresql" or config["HOST"] not in {"127.0.0.1", "::1"}
        or not str(config["PORT"]).isdigit() or int(config["PORT"]) in {0, 5432}
        or not test_name.startswith("test_") or test_name == f"test_{PROTECTED_DB}"
        or config["NAME"] != (test_name if migrate else "postgres")
    ):
        raise CommandError("PostgreSQL tests require a loopback test cluster on a separate port and a dedicated test_ database.")
    confirm_postgres(connection)
