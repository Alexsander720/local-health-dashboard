import unittest
from pathlib import Path

from build_dashboard import render_html


class DashboardV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = render_html(
            include_ai=False,
            server_mode=True,
            demo_mode=True,
        )

    def test_renders_v2_shell_and_sidebar_navigation(self):
        self.assertIn('class="app-shell"', self.html)
        self.assertIn('class="sidebar"', self.html)
        self.assertIn('data-tab="sleep"', self.html)
        self.assertIn('data-tab="notes"', self.html)

    def test_uses_svg_icon_sprite_instead_of_emoji_tabs(self):
        self.assertIn('id="icon-sleep"', self.html)
        self.assertIn('<svg class="ui-icon"', self.html)
        self.assertNotIn('>🌙 Сон</button>', self.html)
        self.assertNotIn('>❤️ Здоровье</button>', self.html)

    def test_exposes_section_accents_and_real_sleep_metrics(self):
        self.assertIn('--section-accent', self.html)
        self.assertIn('class="sleep-metrics"', self.html)
        self.assertIn('data-sleep-metric="sleep-debt"', self.html)
        self.assertIn('data-sleep-metric="sleep-regularity"', self.html)
        self.assertIn('data-sleep-metric="sleep-window"', self.html)
        self.assertIn('data-sleep-metric="sleep-action"', self.html)
        self.assertNotIn('class="future-metrics"', self.html)

    def test_top_kpis_follow_the_active_section(self):
        source = Path("build_dashboard.py").read_text(encoding="utf-8")
        self.assertIn('id="sectionStats"', self.html)
        self.assertIn("const SECTION_KPIS =", self.html)
        self.assertIn("renderSectionStats(name)", self.html)
        self.assertIn("section_kpis_json", source)
        self.assertNotIn("<!-- Stats -->", self.html)

    def test_sleep_metrics_are_explained_instead_of_presented_as_diagnosis(self):
        self.assertIn("Расчётный долг", self.html)
        self.assertIn("Ориентир", self.html)
        self.assertIn("записанных ночей", self.html)
        self.assertIn("не закрывать за одну ночь", self.html)
        self.assertIn("sleep-confidence", self.html)

    def test_sleep_metrics_use_a_dedicated_domain_payload(self):
        source = Path("build_dashboard.py").read_text(encoding="utf-8")
        self.assertIn("const SLEEP_METRICS =", self.html)
        self.assertIn("sleep_metrics_json", source)
        self.assertIn("compute_sleep_metrics", source)

    def test_supports_section_deep_links(self):
        self.assertIn("window.location.hash.slice(1)", self.html)
        self.assertIn("history.replaceState(null, '', '#' + name)", self.html)
        self.assertIn("window.addEventListener('hashchange'", self.html)

    def test_uses_shared_tabler_icon_sprite(self):
        self.assertIn('data-icon-set="tabler-icons"', self.html)
        self.assertIn('id="icon-chart-donut"', self.html)
        self.assertIn('id="icon-lungs"', self.html)

    def test_sections_expose_structured_layout_roles(self):
        for class_name in (
            "section-insight",
            "primary-chart",
            "secondary-chart",
            "summary-chart",
            "nutrition-diary-card",
            "nutrition-balance",
            "nutrition-calories",
            "nutrition-macros",
            "nutrition-ideas",
            "body-measurements",
            "food-profile-main",
            "food-profile-summary",
        ):
            self.assertIn(class_name, self.html)

    def test_nutrition_hides_unavailable_balance_and_expands_calories(self):
        self.assertIn("const hasEnergyBalance", self.html)
        self.assertIn("balanceCard.classList.add('is-unavailable')", self.html)
        self.assertIn("calorieCard.classList.add('is-wide')", self.html)
        self.assertIn(".nutrition-balance.is-unavailable", self.html)
        self.assertIn(".nutrition-calories.is-wide", self.html)

    def test_nutrition_without_goals_has_explicit_compact_state(self):
        self.assertIn('id="nutritionDays"', self.html)
        self.assertIn("const hasCalorieGoal = Boolean", self.html)
        self.assertIn("diary-goal-state", self.html)
        self.assertIn("Цель калорий не задана", self.html)

    def test_nutrition_uses_a_clear_primary_workspace(self):
        self.assertIn("nutrition-action-panel", self.html)
        self.assertIn("#tab-nutrition .nutrition-diary-card", self.html)
        self.assertIn("grid-column:1/9", self.html)
        self.assertIn("#tab-nutrition .nutrition-ideas", self.html)
        self.assertIn("grid-column:9/-1", self.html)
        self.assertIn("max-height:640px", self.html)
        self.assertIn("overflow:auto", self.html)
        self.assertNotIn("height:600px", self.html)

    def test_nutrition_removes_unavailable_charts_from_layout(self):
        self.assertIn("balanceCard.hidden = true", self.html)
        self.assertIn("calorieCard.classList.add('is-wide')", self.html)

    def test_nutrition_controls_use_shared_svg_icons(self):
        self.assertIn('id="icon-arrow-left"', self.html)
        self.assertIn('id="icon-sunrise"', self.html)
        self.assertIn('<use href="#icon-arrow-left"></use>', self.html)
        self.assertNotIn("🌅 Завтрак", self.html)
        self.assertNotIn("🍿 Перекус", self.html)

    def test_sanitizes_every_model_generated_html_surface(self):
        self.assertIn("function sanitizeAiHtml", self.html)
        self.assertIn("modalContent.innerHTML = sanitizeAiHtml(cache.text)", self.html)
        self.assertIn("result.innerHTML = sanitizeAiHtml(j.text)", self.html)
        self.assertIn("sanitizeAiHtml(html)", self.html)

    def test_category_ai_cards_use_concise_summary_instead_of_clipped_full_text(self):
        self.assertIn("body.textContent = extractSummary(cache.text)", self.html)
        self.assertNotIn("body.innerHTML = cache.text", self.html)
        self.assertNotIn("mask-image:linear-gradient(to bottom,#000 0%,#000 82%,transparent 100%)", self.html)

    def test_desktop_section_insights_are_compact_full_width_strips(self):
        self.assertIn(".tab-panel > .section-insight", self.html)
        self.assertIn("grid-column:1/-1", self.html)
        self.assertIn("height:auto", self.html)
        self.assertNotIn("height:382px;\n    display:flex;\n    flex-direction:column;\n}}", self.html)

    def test_chat_controls_use_shared_svg_icons(self):
        self.assertIn('class="chat-fab"', self.html)
        self.assertIn('aria-label="Открыть AI-ассистента"', self.html)
        self.assertIn('<use href="#icon-message"></use>', self.html)
        self.assertIn('<use href="#icon-trash"></use>', self.html)
        self.assertIn('<use href="#icon-close"></use>', self.html)

    def test_mobile_chat_control_lives_in_header_instead_of_covering_content(self):
        head_start = self.html.index('<div class="head-controls">')
        head_end = self.html.index("</header>", head_start)
        chat_button = self.html.index('id="chatFab"')
        self.assertLess(head_start, chat_button)
        self.assertLess(chat_button, head_end)
        self.assertIn(".head-controls .chat-fab", self.html)
        self.assertIn("position:static", self.html)

    def test_exposes_data_source_health_panel(self):
        self.assertIn('id="dataHealthBtn"', self.html)
        self.assertIn('id="dataHealthModal"', self.html)
        self.assertIn('id="dataHealthSources"', self.html)
        self.assertIn("function renderSourceHealth(status)", self.html)
        self.assertIn("status.sources", self.html)
        self.assertIn("status.jobs", self.html)
        self.assertIn("function refreshRuntimeStatus()", self.html)
        self.assertIn("closeDataHealth();", self.html)


if __name__ == "__main__":
    unittest.main()
