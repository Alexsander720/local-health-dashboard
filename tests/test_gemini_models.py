import unittest
from datetime import datetime, timedelta

from gemini_analyzer import DEFAULT_MODEL, build_payload, endpoint_host, model_location


class GeminiModelRoutingTests(unittest.TestCase):
    def test_defaults_prefer_gemini_31_pro_preview(self):
        self.assertEqual(DEFAULT_MODEL["week"], "gemini-3.1-pro-preview")
        self.assertEqual(DEFAULT_MODEL["month"], "gemini-3.1-pro-preview")
        self.assertEqual(DEFAULT_MODEL["nutrition"], "gemini-3.1-pro-preview")

    def test_gemini_31_uses_global_endpoint(self):
        self.assertEqual(model_location("gemini-3.1-pro-preview"), "global")
        self.assertEqual(endpoint_host("global"), "aiplatform.googleapis.com")

    def test_legacy_models_keep_regional_endpoint(self):
        self.assertEqual(model_location("gemini-2.5-pro"), "us-central1")
        self.assertEqual(endpoint_host("us-central1"), "us-central1-aiplatform.googleapis.com")

    def test_nutrition_payload_includes_food_names_for_cooking_advice(self):
        today = datetime.now().strftime("%Y-%m-%d")
        data = {
            "nutrition": [
                {
                    "date": today,
                    "total_kcal": 1200,
                    "protein_g": 40,
                    "fat_g": 50,
                    "carb_g": 160,
                    "meals": {
                        "breakfast": {
                            "items": [
                                {"name": "Овсяная каша", "kcal": 300},
                                {"name": "Куриная отбивная", "kcal": 250},
                            ]
                        }
                    },
                }
            ]
        }

        payload = build_payload(data, "nutrition")

        self.assertIn("items", payload["nutrition"][0])
        self.assertIn("Овсяная каша", payload["nutrition"][0]["items"])

    def test_activity_payload_includes_google_fit_workouts(self):
        today = datetime.now().strftime("%Y-%m-%d")
        data = {
            "daily_metrics": [],
            "workouts": [
                {
                    "date": today,
                    "datetime": f"{today} 15:20:32",
                    "training": "Strengthtraining",
                    "duration_min": 69,
                    "kcal": 383.3,
                    "source": "GoogleFit",
                }
            ],
        }

        payload = build_payload(data, "activity")

        self.assertEqual(payload["workouts"][0]["source"], "GoogleFit")
        self.assertEqual(payload["workouts"][0]["duration_min"], 69)

    def test_activity_payload_includes_kingsmith_treadmill_context(self):
        today = datetime.now().strftime("%Y-%m-%d")
        data = {
            "daily_metrics": [],
            "workouts": [
                {
                    "date": today,
                    "datetime": f"{today} 01:46:11",
                    "training": "walking_treadmill",
                    "training_ru": "Ходьба на дорожке",
                    "duration_min": 4.8,
                    "kcal": 8.0,
                    "steps": 225,
                    "distance_km": 0.15,
                    "avg_speed_kmh": 1.9,
                    "fragment_count": 3,
                    "source": "HealthConnect",
                    "source_app": "KS Fit",
                }
            ],
        }

        payload = build_payload(data, "activity")

        workout = payload["workouts"][0]
        self.assertEqual(workout["training"], "Ходьба на дорожке")
        self.assertEqual(workout["source_app"], "KS Fit")
        self.assertEqual(workout["avg_speed_kmh"], 1.9)
        self.assertEqual(workout["fragment_count"], 3)

    def test_foodprofile_period_uses_gemini_31(self):
        self.assertEqual(DEFAULT_MODEL["foodprofile"], "gemini-3.1-pro-preview")

    def test_chat_payload_includes_full_available_history_and_coverage(self):
        today = datetime.now().date()
        recent = today.strftime("%Y-%m-%d")
        old = (today - timedelta(days=45)).strftime("%Y-%m-%d")
        data = {
            "nutrition": [
                {"date": recent, "total_kcal": 1800, "protein_g": 90, "fat_g": 60, "carb_g": 180, "meals": {}},
                {"date": old, "total_kcal": 1500, "protein_g": 70, "fat_g": 50, "carb_g": 160, "meals": {}},
            ],
            "daily_metrics": [
                {"date": old, "activity": {"steps": 1200, "calories": 300, "active_min": 20}},
            ],
            "workouts": [
                {"date": old, "training": "walking", "duration_min": 30, "kcal": 120},
            ],
            "weight_history": [
                {"date": recent, "weight_kg": 97.2},
                {"date": old, "weight_kg": 95.0},
            ],
        }

        payload = build_payload(data, "chat")

        self.assertIn(old, [row["date"] for row in payload["nutrition"]])
        self.assertIn(old, [row["date"] for row in payload["daily_metrics"]])
        self.assertIn(old, [row["date"] for row in payload["workouts"]])
        self.assertIn(old, [row["date"] for row in payload["weight_history"]])
        self.assertEqual(payload["data_coverage"]["nutrition"]["first_date"], old)
        self.assertEqual(payload["data_coverage"]["nutrition"]["last_date"], recent)
        self.assertEqual(payload["data_coverage"]["nutrition"]["count"], 2)


if __name__ == "__main__":
    unittest.main()
