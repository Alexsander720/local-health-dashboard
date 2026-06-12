"""Tests for requesting fresh health data from Mobvoi Companion."""
from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import health_sync
from health_sync import _run, _wake_mobvoi_via_adb


class MobvoiWakeTests(unittest.TestCase):
    @patch("health_sync._get_adb_device_serials", return_value=["192.168.50.72:5555"])
    @patch("health_sync._run")
    def test_starts_health_activity_that_requests_watch_data(self, run, _serials):
        run.return_value = Mock(returncode=0, stdout="Status: ok\n", stderr="")

        self.assertTrue(_wake_mobvoi_via_adb())

        command = run.call_args.args[0]
        self.assertIn("com.mobvoi.health.companion.HealthActivity", command[-1])
        self.assertNotIn("monkey", command)

    @patch("health_sync._subprocess_run")
    @patch("health_sync.os.name", "nt")
    def test_run_hides_child_console_windows(self, subprocess_run):
        subprocess_run.return_value = Mock(returncode=0)

        _run(["adb", "devices"], creationflags=4)

        flags = subprocess_run.call_args.kwargs["creationflags"]
        self.assertEqual(flags, 4 | health_sync._NO_WINDOW)


if __name__ == "__main__":
    unittest.main()
