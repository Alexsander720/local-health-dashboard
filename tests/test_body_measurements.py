import unittest

from health_dashboard.domain.body import normalize_measurements, validate_measurements


class BodyMeasurementTests(unittest.TestCase):
    def test_wraps_legacy_flat_measurement_under_fallback_date(self):
        normalized = normalize_measurements(
            {"waist_cm": 100, "hips_cm": 111},
            fallback_date="2026-06-06",
        )

        self.assertEqual(
            normalized,
            {"2026-06-06": {"waist_cm": 100.0, "hips_cm": 111.0}},
        )

    def test_preserves_canonical_measurement_history(self):
        history = {
            "2026-05-01": {
                "chest_cm": 106,
                "waist_cm": 110,
                "added_at": "2026-05-01T04:50:00",
            },
            "2026-06-06": {"waist_cm": 100},
        }

        self.assertEqual(normalize_measurements(history), history)
        self.assertEqual(validate_measurements(history), history)

    def test_rejects_flat_payload_for_persistence(self):
        with self.assertRaisesRegex(ValueError, "date-keyed"):
            validate_measurements({"waist_cm": 100})


if __name__ == "__main__":
    unittest.main()
