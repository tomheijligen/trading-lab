"""Isolated test configuration. Never connects to the configured PostgreSQL database."""
import os

os.environ.setdefault("TRADING_LOCAL", "1")
os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-" + "x" * 50)
os.environ.setdefault("PGPASSWORD", "unused")

from .settings import *  # noqa: F403

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
