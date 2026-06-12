import unittest
from pathlib import Path

import build_dashboard


class DashboardRenderingTests(unittest.TestCase):
    def test_server_html_render_does_not_rewrite_static_dashboard_file(self):
        original = build_dashboard.HTML_PATH.read_text(encoding="utf-8") if build_dashboard.HTML_PATH.exists() else ""
        try:
            build_dashboard.build(include_ai=True, server_mode=False)
            before = build_dashboard.HTML_PATH.read_text(encoding="utf-8")

            html = build_dashboard.render_html(include_ai=True, server_mode=True)
            after = build_dashboard.HTML_PATH.read_text(encoding="utf-8")

            self.assertIn("const SERVER_MODE = true;", html)
            self.assertIn("const SERVER_MODE = false;", before)
            self.assertEqual(before, after)
        finally:
            if original:
                build_dashboard.HTML_PATH.write_text(original, encoding="utf-8")

    def test_workout_share_map_surface_is_rendered(self):
        html = build_dashboard.render_html(include_ai=False, server_mode=True)

        self.assertIn('id="workoutShareModal"', html)
        self.assertIn('class="btn sm workout-share-btn"', html)
        self.assertIn("function shareWorkout(", html)
        self.assertIn("function drawShareMap(", html)
        self.assertIn("shareMapCanvas", html)
        self.assertIn("shareDistanceLabel", html)
        self.assertIn("function workoutHasGpsRoute(", html)
        self.assertIn("const shareHtml = workoutHasGpsRoute(w)", html)
        self.assertIn(">Карта</button>", html)
        self.assertIn("Карта тренировки", html)
        self.assertNotIn(">Поделиться</button>", html)
        self.assertIn("workoutVisibleCount", html)
        self.assertIn("Показать все", html)
        self.assertIn("С картой", html)
        self.assertIn("if (!route.gps) return;", html)
        self.assertNotIn("function makeSchematicRoute(", html)
        self.assertNotIn("Схема без GPS", html)
        self.assertNotIn("GPS-маршрут не найден", html)
        self.assertNotIn("не рисую фейковую дорожку", html)


if __name__ == "__main__":
    unittest.main()
