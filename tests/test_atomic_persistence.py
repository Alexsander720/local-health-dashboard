import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build_dashboard
import gemini_analyzer
import health_sync
import ksfit_sync
import note


class AtomicPersistenceTests(unittest.TestCase):
    def test_sync_dataset_and_yazio_archive_use_atomic_json(self):
        with patch("storage_utils.atomic_write_json") as atomic_json:
            health_sync.save_sync_data({"synced_at": "2026-06-07"})
            health_sync._save_yazio_notes_archive({"2026-06-07": {"note": "ok"}})

        self.assertEqual(atomic_json.call_count, 2)
        self.assertEqual(atomic_json.call_args_list[0].args[0], health_sync.JSON_PATH)
        self.assertEqual(atomic_json.call_args_list[1].args[0], health_sync.YAZIO_NOTES_ARCHIVE_PATH)

    def test_ai_cache_writes_text_and_metadata_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            text_path = Path(tmp) / "cache.txt"
            meta_path = Path(tmp) / "cache.meta.json"
            with patch.dict(gemini_analyzer.CACHE_PATHS, {"sleep": (text_path, meta_path)}), \
                    patch("storage_utils.atomic_write_text") as atomic_text, \
                    patch("storage_utils.atomic_write_json") as atomic_json:
                gemini_analyzer.save_cache("sleep", "<p>ok</p>", "model", "project")

        atomic_text.assert_called_once_with(text_path, "<p>ok</p>")
        self.assertEqual(atomic_json.call_args.args[0], meta_path)
        self.assertEqual(atomic_json.call_args.args[1]["period"], "sleep")

    def test_dashboard_profile_and_html_use_atomic_writes(self):
        with patch("storage_utils.atomic_write_json") as atomic_json, \
                patch("storage_utils.atomic_write_text") as atomic_text, \
                patch("build_dashboard.render_html", return_value="<html>ok</html>"):
            build_dashboard.save_food_profile({"notes": "test"})
            build_dashboard.build()

        self.assertEqual(atomic_json.call_args.args[0], build_dashboard.FOOD_PROFILE_PATH)
        atomic_text.assert_called_once_with(build_dashboard.HTML_PATH, "<html>ok</html>")

    def test_ksfit_archive_and_cli_notes_use_atomic_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "archive.json"
            with patch("storage_utils.atomic_write_json") as atomic_json:
                ksfit_sync._save_archive({1: {"run_id": 1, "_start_ms": 1}}, archive)
                note.save_notes({"2026-06-07": [{"text": "ok"}]})

        self.assertEqual(atomic_json.call_count, 2)
        self.assertEqual(atomic_json.call_args_list[0].args[0], archive)
        self.assertEqual(atomic_json.call_args_list[1].args[0], note.NOTES_PATH)
