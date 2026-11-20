"""PostgreSQL concurrency tests for production row-lock behaviour."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection, connections
from django.test import TransactionTestCase

from .market_data import DataError, Provider


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
class SharedQuoteConcurrencyTests(TransactionTestCase):
    @patch("trading.market_data.read_json")
    def test_worker_and_web_share_one_inflight_request(self, read_json):
        active, finished = Event(), Event()
        provider = Provider("Test", "https://example.invalid/", 5)

        def respond(*_args):
            active.set()
            if not finished.wait(5):
                raise TimeoutError
            return {"close": "100"}

        def fetch():
            return provider.fetch("quote", {"symbol": "BTC/EUR"}, {}, ttl=timedelta(minutes=5))

        def worker():
            try:
                return fetch()
            finally:
                connections.close_all()

        read_json.side_effect = respond
        with ThreadPoolExecutor(max_workers=1) as executor:
            promise = executor.submit(worker)
            try:
                self.assertTrue(active.wait(5))
                with self.assertRaises(DataError):
                    fetch()
            finally:
                finished.set()
            self.assertEqual(promise.result(timeout=5), {"close": "100"})
        self.assertEqual(fetch(), {"close": "100"})
        read_json.assert_called_once()
