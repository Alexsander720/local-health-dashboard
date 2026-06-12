import unittest

from health_dashboard.domain.sleep import aggregate_sleep_by_date, compute_sleep_metrics


def sleep_row(date, bedtime, waketime, sleep_min, duration_min=None):
    return {
        "date": date,
        "bedtime": bedtime,
        "waketime": waketime,
        "sleep_min": sleep_min,
        "duration_min": duration_min if duration_min is not None else sleep_min,
        "stages": {
            "deep": {"min": sleep_min * 0.15},
            "rem": {"min": sleep_min * 0.20},
            "light": {"min": sleep_min * 0.65},
            "awake": {"min": max(0, (duration_min or sleep_min) - sleep_min)},
        },
    }


class SleepAggregationTests(unittest.TestCase):
    def test_aggregate_preserves_actual_sleep_separately_from_time_in_bed(self):
        rows = [
            sleep_row("2026-06-06", "23:40", "03:40", 220, 240),
            sleep_row("2026-06-06", "04:00", "08:00", 230, 240),
        ]

        merged = aggregate_sleep_by_date(rows)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["sleep_min"], 450)
        self.assertEqual(merged[0]["duration_min"], 480)


class SleepMetricsTests(unittest.TestCase):
    def test_debt_uses_recorded_nights_and_allows_surplus_to_offset_deficit(self):
        rows = [
            sleep_row("2026-06-04", "00:00", "07:00", 420),
            sleep_row("2026-06-03", "00:00", "08:00", 480),
            sleep_row("2026-06-02", "00:00", "06:30", 390),
            sleep_row("2026-06-01", "00:00", "07:30", 450),
        ]

        metrics = compute_sleep_metrics(rows, target_min=450, recent_nights=7)

        self.assertEqual(metrics["recent_nights_count"], 4)
        self.assertEqual(metrics["avg_sleep_min"], 435)
        self.assertEqual(metrics["debt_min"], 60)
        self.assertEqual(metrics["confidence"], "medium")
        self.assertEqual(metrics["recommended_sleep_min"], 465)

    def test_debt_uses_actual_sleep_instead_of_total_duration(self):
        rows = [
            sleep_row("2026-06-02", "00:00", "08:00", 420, 480),
            sleep_row("2026-06-01", "00:00", "08:00", 420, 480),
        ]

        metrics = compute_sleep_metrics(rows, target_min=450)

        self.assertEqual(metrics["debt_min"], 60)
        self.assertEqual(metrics["latest_sleep_min"], 420)

    def test_regularity_treats_times_across_midnight_as_nearby(self):
        rows = [
            sleep_row("2026-06-03", "23:50", "07:50", 470),
            sleep_row("2026-06-02", "00:10", "08:10", 470),
            sleep_row("2026-06-01", "23:55", "08:00", 470),
        ]

        metrics = compute_sleep_metrics(rows, target_min=450)

        self.assertGreaterEqual(metrics["regularity_score"], 90)
        self.assertLessEqual(metrics["bedtime_spread_min"], 10)
        self.assertLessEqual(metrics["waketime_spread_min"], 10)
        self.assertIn(metrics["typical_bedtime"], {"23:58", "23:59", "00:00"})
        self.assertIn(metrics["typical_waketime"], {"07:59", "08:00", "08:01"})

    def test_regularity_is_not_claimed_from_two_nights(self):
        rows = [
            sleep_row("2026-06-02", "01:00", "08:00", 420),
            sleep_row("2026-06-01", "01:20", "08:20", 420),
        ]

        metrics = compute_sleep_metrics(rows, target_min=450)

        self.assertIsNone(metrics["regularity_score"])
        self.assertEqual(metrics["confidence"], "low")
        self.assertEqual(metrics["regularity_label"], "мало данных")


if __name__ == "__main__":
    unittest.main()
