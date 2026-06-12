import threading
import unittest
from datetime import datetime, timezone

from runtime_state import JobRegistry, build_source_status


class JobRegistryTests(unittest.TestCase):
    def test_rejects_duplicate_job_while_first_run_is_active(self):
        registry = JobRegistry()
        started = threading.Event()
        release = threading.Event()
        first_result = {}

        def work():
            started.set()
            release.wait(timeout=2)
            return {"ok": True}

        thread = threading.Thread(
            target=lambda: first_result.update(registry.run("sync", work)),
            daemon=True,
        )
        thread.start()
        self.assertTrue(started.wait(timeout=2))

        duplicate = registry.run("sync", lambda: {"ok": False})

        self.assertFalse(duplicate["accepted"])
        self.assertEqual(duplicate["job"]["status"], "running")
        release.set()
        thread.join(timeout=2)
        self.assertTrue(first_result["accepted"])
        self.assertEqual(first_result["result"], {"ok": True})
        self.assertEqual(registry.snapshot()["sync"]["status"], "idle")
        self.assertTrue(registry.snapshot()["sync"]["last_ok"])

    def test_records_failed_job_without_leaving_it_running(self):
        registry = JobRegistry()

        with self.assertRaisesRegex(RuntimeError, "boom"):
            registry.run("ai:sleep", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

        state = registry.snapshot()["ai:sleep"]
        self.assertEqual(state["status"], "idle")
        self.assertFalse(state["last_ok"])
        self.assertEqual(state["last_error"], "boom")


class SourceStatusTests(unittest.TestCase):
    def test_builds_source_level_freshness_and_counts(self):
        now = datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
        data = {
            "sync_status": {"phone_online": True, "used_cached_dbs": False},
            "sleep_sessions": [{"end": "2026-06-07T08:00:00+00:00"}],
            "daily_metrics": [{"date": "2026-06-07"}],
            "weight_history": [{"date": "2026-05-20", "time": "09:00"}],
            "nutrition": [{"date": "2026-06-06"}],
            "feelings": [
                {"date": "2026-06-06", "yazio_note": "ok"},
                {"date": "2026-06-07", "manual_note": "manual"},
            ],
            "workouts": [
                {
                    "datetime": "2026-06-06 18:00:00",
                    "source": "HealthConnect",
                    "source_app": "Google Fit",
                },
                {
                    "datetime": "2026-06-05 12:00:00",
                    "source": "ksfit_native",
                    "source_app": "KS Fit",
                },
            ],
        }

        status = build_source_status(data, now=now)

        self.assertEqual(status["mobvoi"]["state"], "fresh")
        self.assertEqual(status["mobvoi"]["records"], 2)
        self.assertEqual(status["zepp_scale"]["state"], "stale")
        self.assertEqual(status["yazio"]["records"], 2)
        self.assertEqual(status["health_connect"]["records"], 1)
        self.assertEqual(status["google_fit"]["records"], 1)
        self.assertEqual(status["ks_fit"]["records"], 1)
        self.assertEqual(status["manual"]["records"], 1)

    def test_marks_remote_sources_cached_when_sync_used_local_databases(self):
        data = {
            "sync_status": {"phone_online": False, "used_cached_dbs": True},
            "sleep_sessions": [{"date": "2026-06-07"}],
            "daily_metrics": [],
            "weight_history": [],
            "nutrition": [],
            "feelings": [],
            "workouts": [],
        }

        status = build_source_status(data, now=datetime(2026, 6, 7, tzinfo=timezone.utc))

        self.assertEqual(status["mobvoi"]["state"], "cached")
        self.assertEqual(status["manual"]["state"], "missing")


if __name__ == "__main__":
    unittest.main()
