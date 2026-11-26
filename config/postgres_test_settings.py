"""Explicit local PostgreSQL test cluster. Django creates a separate database."""
import os

from .test_settings import *  # noqa: F403

DATABASES = {"default": {
    "ENGINE": "django.db.backends.postgresql",
    "HOST": os.environ["PGTESTHOST"],
    "PORT": os.environ["PGTESTPORT"],
    "USER": os.environ["PGTESTUSER"],
    "PASSWORD": os.environ["PGTESTPASSWORD"],
    "NAME": "postgres",
    "TEST": {"NAME": os.environ["PGTESTDATABASE"]},
}}
