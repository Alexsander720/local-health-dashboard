"""Regression tests for Mobvoi sleep sessions missing from data_session."""
from __future__ import annotations

import unittest

from health_sync import _infer_sleep_sessions, _summarize_sleep_stages


MINUTE = 60_000


class SleepSessionInferenceTests(unittest.TestCase):
    def test_infers_completed_session_and_fills_missing_transition_interval(self):
        start = 1_780_630_000_000
        rows = [
            ("7,9", start, start + 10 * MINUTE),
            ("10,9", start + 370 * MINUTE, start + 375 * MINUTE),
            ("9,99", start + 375 * MINUTE, start + 380 * MINUTE),
        ]

        sessions = _infer_sleep_sessions(rows, [])
        stage_mins, timeline = _summarize_sleep_stages(rows)

        self.assertEqual(sessions, [(f"inferred:{start}:{start + 380 * MINUTE}", start, start + 380 * MINUTE)])
        self.assertAlmostEqual(stage_mins[7], 10)
        self.assertAlmostEqual(stage_mins[9], 365)
        self.assertAlmostEqual(stage_mins[10], 5)
        self.assertTrue(any(item.get("inferred") and item["stage"] == "light" for item in timeline))

    def test_ignores_unfinished_short_and_existing_sessions(self):
        start = 1_780_630_000_000
        unfinished = [
            ("7,9", start, start + 10 * MINUTE),
            ("9,10", start + 10 * MINUTE, start + 40 * MINUTE),
        ]
        short = [("7,99", start + 60 * MINUTE, start + 70 * MINUTE)]
        completed = [
            ("7,9", start + 120 * MINUTE, start + 130 * MINUTE),
            ("9,99", start + 130 * MINUTE, start + 180 * MINUTE),
        ]

        self.assertEqual(_infer_sleep_sessions(unfinished, []), [])
        self.assertEqual(_infer_sleep_sessions(short, []), [])
        self.assertEqual(
            _infer_sleep_sessions(completed, [("existing", start + 100 * MINUTE, start + 200 * MINUTE)]),
            [],
        )


if __name__ == "__main__":
    unittest.main()
