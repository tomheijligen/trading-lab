from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse


class WorkerTests(TestCase):
    def test_health_queries_database(self):
        self.assertEqual(self.client.get(reverse("health")).json(), {"status": "ok"})

    @override_settings(PLATFORM_LAB_ENABLED=True)
    @patch("trading.management.commands.run_worker.call_command")
    def test_paper_cycle_runs_headless_and_updates_heartbeat(self, command):
        with TemporaryDirectory(dir=".") as directory:
            heartbeat = Path(directory) / "heartbeat"
            call_command("run_worker", "paper", "--once", "--heartbeat", str(heartbeat))
            self.assertTrue(heartbeat.exists())
        self.assertEqual([call.args[0] for call in command.call_args_list], ["run_bots", "refresh_scanner"])
