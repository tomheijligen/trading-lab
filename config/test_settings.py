"""Isolated test configuration. Never connects to the configured PostgreSQL database."""
import os
from unittest.mock import patch

with patch.dict(os.environ, {
    "TRADING_LOCAL": "1", "DJANGO_SECRET_KEY": "test-only-" + "x" * 50,
    "PGHOST": "127.0.0.1", "PGPORT": "55481", "PGDATABASE": "test_unused",
    "PGUSER": "test_unused", "PGPASSWORD": "test-unused",
}):
    from .settings import *  # noqa: F403

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
TRADING_ISOLATED_TESTS = True
