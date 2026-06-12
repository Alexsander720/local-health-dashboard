"""Tests for requesting fresh health data from Mobvoi Companion."""
from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from health_sync import _wake_mobvoi_via_adb


class MobvoiWakeTests(unittest.TestCase):
    @patch("health_sync._get_adb_device_serials", return_value=["192.168.50.72:5555"])
    @patch("health_sync.subprocess.run")
    def test_starts_health_activity_that_requests_watch_data(self, run, _serials):
        run.return_value = Mock(returncode=0, stdout="Status: ok\n", stderr="")

        self.assertTrue(_wake_mobvoi_via_adb())

        command = run.call_args.args[0]
        self.assertIn("com.mobvoi.health.companion.HealthActivity", command[-1])
        self.assertNotIn("monkey", command)


if __name__ == "__main__":
    unittest.main()
