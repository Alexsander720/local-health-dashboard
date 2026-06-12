import unittest

from build_dashboard import render_html
from health_dashboard.domain.overview import compute_overview


class OverviewDomainTests(unittest.TestCase):
    def test_prioritizes_sleep_then_movement_then_protein(self):
        data = {
            "nutrition_goals": {"protein_g": 130, "steps_goal": 9000},
            "daily_metrics": [
                {"date": "2026-06-12", "activity": {"steps": 3100, "active_min": 18}},
            ],
            "nutrition": [
                {"date": "2026-06-12", "total_kcal": 1700, "protein_g": 62},
            ],
            "weight_history": [
                {"date": "2026-06-12", "weight_kg": 84.5},
                {"date": "2026-05-10", "weight_kg": 86.0},
            ],
        }
        overview = compute_overview(
            data,
            sleep_metrics={
                "target_min": 450,
                "latest_sleep_min": 330,
                "avg_sleep_min": 360,
                "debt_min": 540,
                "regularity_score": 58,
            },
        )

        self.assertIn("восстановление", overview["headline"].lower())
        self.assertEqual(
            [action["key"] for action in overview["actions"][:3]],
            ["sleep", "movement", "protein"],
        )
        self.assertEqual(len(overview["signals"]), 3)
        self.assertEqual(len(overview["trends"]), 3)

    def test_healthy_inputs_produce_maintenance_message(self):
        data = {
            "nutrition_goals": {"protein_g": 120, "steps_goal": 8000},
            "daily_metrics": [
                {"date": "2026-06-12", "activity": {"steps": 9200, "active_min": 55}},
            ],
            "nutrition": [
                {"date": "2026-06-12", "total_kcal": 2050, "protein_g": 126},
            ],
            "weight_history": [{"date": "2026-06-12", "weight_kg": 80}],
        }
        overview = compute_overview(
            data,
            sleep_metrics={
                "target_min": 450,
                "latest_sleep_min": 465,
                "avg_sleep_min": 452,
                "debt_min": 0,
                "regularity_score": 88,
            },
        )

        self.assertIn("держи курс", overview["headline"].lower())
        self.assertEqual(overview["actions"][0]["key"], "maintain")

    def test_single_action_uses_singular_summary(self):
        data = {
            "nutrition_goals": {"protein_g": 120, "steps_goal": 8000},
            "daily_metrics": [
                {"date": "2026-06-12", "activity": {"steps": 5000, "active_min": 32}},
            ],
            "nutrition": [
                {"date": "2026-06-12", "total_kcal": 2050, "protein_g": 126},
            ],
        }
        overview = compute_overview(
            data,
            sleep_metrics={
                "target_min": 450,
                "latest_sleep_min": 465,
                "avg_sleep_min": 452,
                "debt_min": 0,
            },
        )

        self.assertIn("1 практический шаг", overview["summary"])


class OverviewRenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = render_html(include_ai=False, server_mode=True, demo_mode=True)

    def test_overview_is_the_default_structured_section(self):
        self.assertIn('data-tab="overview"', self.html)
        self.assertIn('id="tab-overview"', self.html)
        self.assertIn('data-active-section="overview"', self.html)
        self.assertIn('class="overview-hero"', self.html)
        self.assertIn('class="overview-signals"', self.html)
        self.assertIn('class="overview-trends"', self.html)
        self.assertIn('class="overview-actions"', self.html)
        self.assertIn("const OVERVIEW =", self.html)


if __name__ == "__main__":
    unittest.main()
