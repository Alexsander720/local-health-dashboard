import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from http.client import HTTPConnection
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch

import dashboard_server


def request_dashboard(path="/", *, method="GET", body=None, headers=None):
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard_server.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_port}{path}",
            data=body,
            headers=headers or {},
            method=method,
        )
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            return error.code, dict(error.headers), error.read()
        with response:
            return response.status, dict(response.headers), response.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class DashboardServerTests(unittest.TestCase):
    def test_run_sync_rebuilds_static_dashboard_after_successful_sync(self):
        completed = Mock(returncode=0, stdout="sync ok\nlast line", stderr="")

        with patch("dashboard_server.subprocess.run", return_value=completed) as run, \
                patch("build_dashboard.build") as build:
            result = dashboard_server.run_sync()

        self.assertTrue(result["ok"])
        self.assertTrue(result["rebuilt"])
        self.assertIn("--wake-mobvoi", run.call_args.args[0])
        build.assert_called_once_with(include_ai=True, server_mode=False)

    def test_delete_manual_note_removes_one_entry_by_added_at(self):
        notes = {
            "2026-04-26": [
                {"text": "first", "time": "00:01", "added_at": "a"},
                {"text": "second", "time": "00:02", "added_at": "b"},
            ]
        }

        result = dashboard_server.delete_manual_note(notes, "2026-04-26", added_at="a")

        self.assertTrue(result["ok"])
        self.assertEqual(result["deleted"]["text"], "first")
        self.assertEqual(notes["2026-04-26"], [{"text": "second", "time": "00:02", "added_at": "b"}])

    def test_delete_manual_note_requires_selector_when_date_has_many_entries(self):
        notes = {
            "2026-04-26": [
                {"text": "first", "added_at": "a"},
                {"text": "second", "added_at": "b"},
            ]
        }

        result = dashboard_server.delete_manual_note(notes, "2026-04-26")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "selector required")
        self.assertEqual(len(notes["2026-04-26"]), 2)

    def test_chat_system_prompt_uses_full_chat_payload(self):
        data = {"nutrition": [{"date": "2026-04-20"}]}
        payload = {
            "data_coverage": {
                "nutrition": {"count": 1, "first_date": "2026-04-20", "last_date": "2026-04-20"}
            },
            "nutrition": [{"date": "2026-04-20", "kcal": 1800}],
        }

        with patch("gemini_analyzer.build_payload", return_value=payload) as build_payload:
            prompt = dashboard_server.build_chat_system_prompt(data, "2026-05-24 14:03")

        build_payload.assert_called_once_with(data, "chat")
        self.assertIn("2026-04-20", prompt)
        self.assertIn("data_coverage", prompt)

    def test_notes_and_measurements_use_atomic_json_writes(self):
        measurements = {"2026-06-06": {"waist_cm": 100}}
        with patch("storage_utils.atomic_write_json") as atomic_write:
            dashboard_server.save_notes({"2026-06-06": [{"text": "note"}]})
            dashboard_server.save_measurements(measurements)

        self.assertEqual(atomic_write.call_count, 2)
        self.assertEqual(atomic_write.call_args_list[0].args[0], dashboard_server.NOTES_PATH)
        self.assertEqual(atomic_write.call_args_list[1].args[0], dashboard_server.MEASUREMENTS_PATH)
        self.assertEqual(atomic_write.call_args_list[1].args[1], measurements)

    def test_measurements_refuse_flat_payload_that_would_erase_history(self):
        with patch("storage_utils.atomic_write_json") as atomic_write:
            with self.assertRaisesRegex(ValueError, "date-keyed"):
                dashboard_server.save_measurements({"waist_cm": 100})

        atomic_write.assert_not_called()

    def test_rejects_cross_origin_mutating_request(self):
        status, _, body = request_dashboard(
            "/api/unknown",
            method="POST",
            body=b"{}",
            headers={
                "Content-Type": "application/json",
                "Origin": "https://example.com",
            },
        )

        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body), {"error": "cross-origin request rejected"})

    def test_rejects_oversized_json_body(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard_server.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.putrequest("POST", "/api/unknown")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", dashboard_server.MAX_JSON_BODY_BYTES + 1)
            connection.endheaders()
            response = connection.getresponse()
            body = response.read()
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(response.status, 413)
        self.assertEqual(json.loads(body), {"error": "request body too large"})

    def test_demo_mode_serves_synthetic_data(self):
        with patch.object(dashboard_server, "DEMO_MODE", True):
            status, _, body = request_dashboard("/api/data")

        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["user"]["name"], "Demo User")
        self.assertTrue(payload["sync_status"]["demo"])

    def test_demo_mode_rejects_mutating_requests(self):
        with patch.object(dashboard_server, "DEMO_MODE", True):
            status, _, body = request_dashboard(
                "/api/note",
                method="POST",
                body=b'{"date":"2026-06-12","text":"private"}',
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body), {"error": "demo mode is read-only"})

    def test_demo_mode_never_reads_private_sidecar_endpoints(self):
        with patch.object(dashboard_server, "DEMO_MODE", True):
            _, _, measurements_body = request_dashboard("/api/measurements")
            _, _, profile_body = request_dashboard("/api/food-profile")
            _, _, notes_body = request_dashboard("/api/notes")
            _, _, ai_body = request_dashboard("/api/ai?period=week")
            _, _, status_body = request_dashboard("/api/status")

        measurements = json.loads(measurements_body)
        profile = json.loads(profile_body)
        notes = json.loads(notes_body)
        ai = json.loads(ai_body)
        runtime = json.loads(status_body)
        self.assertEqual(len(measurements["measurements"]), 2)
        self.assertEqual(profile["profile"]["source"], "synthetic-demo")
        self.assertTrue(all(item.get("source") == "synthetic-demo" for item in notes))
        self.assertEqual(ai["meta"]["model"], "demo-insight")
        self.assertTrue(runtime["demo"])
        self.assertEqual(set(runtime["sources"]), {"synthetic_demo"})

    def test_security_headers_do_not_allow_wildcard_cors(self):
        status, headers, _ = request_dashboard("/api/unknown")

        self.assertEqual(status, 404)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")
        self.assertEqual(headers.get("Cross-Origin-Resource-Policy"), "same-origin")

    def test_index_error_does_not_expose_traceback(self):
        with patch("build_dashboard.render_html", side_effect=RuntimeError("private-path-marker")):
            status, _, body = request_dashboard("/")

        text = body.decode("utf-8")
        self.assertEqual(status, 500)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("private-path-marker", text)
        self.assertIn("Dashboard render failed", text)

    def test_sync_route_returns_conflict_when_sync_is_already_running(self):
        busy = {"accepted": False, "job": {"name": "sync", "status": "running"}}
        with patch.object(dashboard_server.JOBS, "run", return_value=busy):
            status, _, body = request_dashboard(
                "/api/sync",
                method="POST",
                body=b"{}",
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(status, 409)
        self.assertTrue(json.loads(body)["busy"])

    def test_ai_route_returns_conflict_for_duplicate_period(self):
        busy = {"accepted": False, "job": {"name": "ai:sleep", "status": "running"}}
        with patch.object(dashboard_server.JOBS, "run", return_value=busy):
            status, _, body = request_dashboard(
                "/api/ai/refresh",
                method="POST",
                body=b'{"period":"sleep"}',
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(status, 409)
        self.assertTrue(json.loads(body)["busy"])

    def test_status_exposes_jobs_and_source_freshness(self):
        sample = {
            "synced_at": "2026-06-07T10:00:00+00:00",
            "sync_status": {"phone_online": True, "used_cached_dbs": False},
            "sleep_sessions": [{"date": "2026-06-07"}],
            "daily_metrics": [],
            "weight_history": [],
            "nutrition": [],
            "workouts": [],
            "feelings": [],
        }
        with patch.object(dashboard_server, "JSON_PATH") as json_path, \
                patch.object(dashboard_server.JOBS, "snapshot", return_value={"sync": {"status": "idle"}}):
            json_path.exists.return_value = True
            json_path.read_text.return_value = json.dumps(sample)
            status, _, body = request_dashboard("/api/status")

        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["jobs"]["sync"]["status"], "idle")
        self.assertIn("mobvoi", payload["sources"])
        self.assertEqual(payload["sources"]["mobvoi"]["records"], 1)


if __name__ == "__main__":
    unittest.main()
