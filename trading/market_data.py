"""Shared, persistent market data access. Provider credentials stay in headers."""
import hashlib
import json
import os
from datetime import timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import MarketCache, ProviderUsage


class DataError(Exception):
    pass


def read_json(url, params, headers):
    request = Request(f"{url}?{urlencode(params)}", headers=headers)
    with urlopen(request, timeout=6) as response:
        return json.loads(response.read(750_000))


class Provider:
    def __init__(self, name, base_url, minute_limit, daily_limit=None):
        self.name = name
        self.base_url = base_url
        self.minute_limit = minute_limit
        self.daily_limit = daily_limit

    def fetch(self, endpoint, params, headers, *, ttl, weight=1):
        identity = json.dumps([self.name, endpoint, params], sort_keys=True)
        key = hashlib.sha256(identity.encode()).hexdigest()
        with transaction.atomic():
            cache, _ = MarketCache.objects.select_for_update().get_or_create(
                key=key, defaults={"provider": self.name},
            )
            now = timezone.now()
            if cache.fetched_at and now - cache.fetched_at < ttl:
                return cache.payload
            if cache.retry_at and cache.retry_at > now:
                raise DataError("Market data is being refreshed or temporarily unavailable.")
            usage, _ = ProviderUsage.objects.select_for_update().get_or_create(
                provider=self.name,
            )
            minute = now.replace(second=0, microsecond=0)
            day = now.date()
            if usage.minute != minute:
                usage.minute, usage.minute_credits = minute, 0
            if usage.day != day:
                usage.day, usage.day_credits = day, 0
            if (
                (usage.blocked_until and usage.blocked_until > now)
                or usage.minute_credits + weight > self.minute_limit
                or (self.daily_limit and usage.day_credits + weight > self.daily_limit)
            ):
                raise DataError(f"{self.name} request budget temporarily exhausted.")
            usage.minute_credits += weight
            usage.day_credits += weight
            usage.save()
            lease = now + timedelta(seconds=30)
            cache.retry_at = lease
            cache.save(update_fields=["retry_at"])

        try:
            payload = read_json(self.base_url + endpoint, params, headers)
            if isinstance(payload, dict) and (
                payload.get("status") == "error" or (payload.get("code") or 0) == 429
            ):
                if str(payload.get("code")) == "429":
                    self.backoff(60)
                raise ValueError("Provider rejected the request.")
        except (URLError, TimeoutError, ValueError, OSError) as error:
            ProviderUsage.objects.filter(pk=self.name).update(last_failure=timezone.now())
            if isinstance(error, HTTPError) and error.code in {418, 429}:
                retry = error.headers.get("Retry-After", "")
                self.backoff(min(int(retry), 86400) if retry.isdecimal() else 120)
            raise DataError(f"{self.name} could not supply market data.") from None

        finished = timezone.now()
        MarketCache.objects.filter(pk=cache.pk, retry_at=lease).update(
            payload=payload, fetched_at=finished, retry_at=None,
        )
        ProviderUsage.objects.filter(pk=self.name).update(last_success=finished)
        return payload

    def backoff(self, seconds):
        deadline = timezone.now() + timedelta(seconds=seconds)
        with transaction.atomic():
            usage = ProviderUsage.objects.select_for_update().get(pk=self.name)
            if not usage.blocked_until or usage.blocked_until < deadline:
                usage.blocked_until = deadline
                usage.save(update_fields=["blocked_until"])


TWELVE = Provider("Twelve Data", "https://api.twelvedata.com/", 8, 800)
BINANCE = Provider("Binance", "https://data-api.binance.vision/api/v3/", 120)


def twelve_data(endpoint, **params):
    key = os.getenv("TWELVE_DATA_API_KEY")
    if not key:
        raise DataError("Set TWELVE_DATA_API_KEY to fetch Twelve Data market data.")
    ttl = timedelta(minutes=settings.MARKET_REFRESH_MINUTES)
    if endpoint == "time_series":
        ttl = timedelta(hours=4)
    return TWELVE.fetch(endpoint, params, {"Authorization": f"apikey {key}"}, ttl=ttl)
