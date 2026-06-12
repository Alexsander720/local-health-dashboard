import unittest
from datetime import date

from build_dashboard import render_html
from health_dashboard.demo_data import (
    build_demo_data,
    build_demo_food_profile,
    build_demo_measurements,
)


class DemoModeTests(unittest.TestCase):
    def test_demo_dataset_is_deterministic_and_covers_every_section(self):
        first = build_demo_data(anchor_date=date(2026, 6, 12))
        second = build_demo_data(anchor_date=date(2026, 6, 12))

        self.assertEqual(first, second)
        self.assertEqual(first["user"]["name"], "Demo User")
        self.assertTrue(first["sync_status"]["demo"])
        self.assertGreaterEqual(len(first["sleep_sessions"]), 14)
        self.assertGreaterEqual(len(first["daily_metrics"]), 30)
        self.assertGreaterEqual(len(first["weight_history"]), 30)
        self.assertGreaterEqual(len(first["nutrition"]), 14)
        self.assertGreaterEqual(len(first["feelings"]), 3)
        self.assertGreaterEqual(len(first["workouts"]), 4)

    def test_demo_sidecar_data_contains_no_private_profile(self):
        measurements = build_demo_measurements(anchor_date=date(2026, 6, 12))
        profile = build_demo_food_profile()

        self.assertGreaterEqual(len(measurements), 2)
        self.assertEqual(profile["source"], "synthetic-demo")
        self.assertNotIn("Alex", repr((measurements, profile)))

    def test_demo_renderer_uses_synthetic_identity_and_marks_the_page(self):
        html = render_html(include_ai=False, server_mode=True, demo_mode=True)

        self.assertIn("Demo User", html)
        self.assertIn('data-demo-mode="true"', html)
        self.assertIn("Синтетические данные", html)
        self.assertNotIn("Alex, 26", html)


if __name__ == "__main__":
    unittest.main()
