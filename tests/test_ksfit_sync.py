"""Tests for ksfit_sync (KS Fit native SQLite parsing + archive)."""
from __future__ import annotations

import json
import inspect
import sqlite3
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import ksfit_sync


def _seed_db(path: Path, *, run_id: int = 1779230770, with_points: bool = True) -> None:
    """Create a tiny KS Fit GetStorage-compatible SQLite for tests."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE record(
            _id INTEGER PRIMARY KEY,
            did TEXT, uid TEXT, detailid TEXT, start_time TEXT,
            steps TEXT, time TEXT, dist TEXT, model TEXT, consume TEXT,
            heart TEXT, runId TEXT, mac TEXT, course_name TEXT,
            deviceType TEXT, point_list TEXT, device_name TEXT
        );
        CREATE TABLE sport_report_point(
            _id INTEGER PRIMARY KEY, timeStamp INTEGER, pointTimeStamp INTEGER,
            did TEXT, calorie REAL, time INTEGER, speed REAL, step INTEGER,
            dist INTEGER, spm INTEGER, recordId INTEGER, model TEXT,
            watt INTEGER, resistance INTEGER, rpm1 INTEGER, count INTEGER,
            rate INTEGER, heart INTEGER
        );
        """
    )
    point_list = json.dumps([
        ["0.0", "0", "0", "0", "0.0", "0", str(run_id), "0", "0", "0", "0", "0", "0", str(run_id), "1779230774838", "AA:BB:CC", "C2"],
        ["20.0", "10", "8", "0", "0.0", "48", str(run_id), "0", "0", "0", "0", "0", "0", str(run_id), "1779230782793", "AA:BB:CC", "C2"],
        ["20.0", "21", "18", "10", "0.6", "51", str(run_id), "0", "0", "0", "0", "0", "0", str(run_id), "1779230792872", "AA:BB:CC", "C2"],
    ])
    conn.execute(
        "INSERT INTO record(_id, did, uid, detailid, start_time, steps, time, dist, model, consume, heart, runId, mac, deviceType, point_list, device_name)"
        " VALUES (1, 'AA:BB:CC', '', 'd1', '2026-05-20 01:46:11', '21', '23', '10', 'C2', '500', '0', ?, 'AA:BB:CC', 'walkingpad', ?, 'KS')",
        (str(run_id), point_list),
    )
    if with_points:
        ts0 = 1779230774838
        for i in range(5):
            conn.execute(
                "INSERT INTO sport_report_point(_id, timeStamp, pointTimeStamp, did, calorie, time, speed, step, dist, spm, recordId, model, watt, resistance, rpm1, count, rate, heart)"
                " VALUES (?, ?, ?, 'AA:BB:CC', ?, ?, ?, ?, ?, ?, ?, 'C2', 0, 0, 0, 0, 0, ?)",
                (
                    i + 1, run_id, ts0 + i * 2000,
                    round(0.1 * i, 2), i * 2, 20.0 + i, (i + 1) * 4, i * 5, 45 + i, run_id, 60 + i,
                ),
            )
    conn.commit()
    conn.close()


class KsFitSyncTests(unittest.TestCase):
    def test_source_does_not_embed_a_local_adb_path_or_phone_ip(self):
        source = inspect.getsource(ksfit_sync)

        self.assertNotIn("E:\\\\Scripts", source)
        self.assertNotIn("192.168.31.", source)

    def test_parse_workout_unifies_record_and_series(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "ks.db"
            archive = Path(tmp) / "archive.json"
            _seed_db(db)
            workouts = ksfit_sync.parse_ksfit_workouts(db_path=db, archive_path=archive)
            self.assertEqual(len(workouts), 1)
            w = workouts[0]
            self.assertEqual(w["source"], "ksfit_native")
            self.assertEqual(w["training"], "walking_treadmill")
            self.assertEqual(w["steps"], 21)
            self.assertEqual(w["distance_km"], 0.01)
            self.assertEqual(w["kcal"], 0.5)
            self.assertEqual(w["device_model"], "C2")
            self.assertEqual(w["ksfit_series_source"], "sport_report_point")
            self.assertEqual(len(w["ksfit_series"]), 5)
            self.assertEqual(w["spm_avg"], 47)
            self.assertEqual(w["speed_max_kmh"], 2.4)
            self.assertEqual(w["hr_avg_native"], 62)

    def test_archive_keeps_workouts_after_db_is_purged(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db1 = Path(tmp) / "ks_1.db"
            db2 = Path(tmp) / "ks_2.db"
            archive = Path(tmp) / "archive.json"
            _seed_db(db1, run_id=111)
            ksfit_sync.parse_ksfit_workouts(db_path=db1, archive_path=archive)

            _seed_db(db2, run_id=222)
            merged = ksfit_sync.parse_ksfit_workouts(db_path=db2, archive_path=archive)

            run_ids = sorted(w["run_id"] for w in merged)
            self.assertEqual(run_ids, [111, 222])

    def test_archive_replaces_existing_record_with_fresher_series(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_thin = Path(tmp) / "ks_thin.db"
            db_rich = Path(tmp) / "ks_rich.db"
            archive = Path(tmp) / "archive.json"
            _seed_db(db_thin, with_points=False)
            stub = ksfit_sync.parse_ksfit_workouts(db_path=db_thin, archive_path=archive)
            self.assertEqual(stub[0]["ksfit_series_source"], "point_list")

            _seed_db(db_rich)
            rich = ksfit_sync.parse_ksfit_workouts(db_path=db_rich, archive_path=archive)
            self.assertEqual(rich[0]["ksfit_series_source"], "sport_report_point")


class PreferKsfitOverHcTests(unittest.TestCase):
    def test_swaps_overlapping_kingsmith_hc_fragments(self):
        from health_sync import KS_FIT_PACKAGE, _prefer_ksfit_native_over_hc

        ks_start = 1_700_000_000_000
        ks_end = ks_start + 300_000
        ksfit_workout = {"run_id": 1, "_start_ms": ks_start, "_end_ms": ks_end, "source": "ksfit_native"}
        hc_fragment = {
            "id": "hc_1",
            "_start_ms": ks_start + 30_000,
            "_end_ms": ks_end - 30_000,
            "source_package": KS_FIT_PACKAGE,
        }
        unrelated = {
            "id": "hc_2",
            "_start_ms": ks_end + 600_000,
            "_end_ms": ks_end + 900_000,
            "source_package": "com.example.tracker",
        }

        out = _prefer_ksfit_native_over_hc([ksfit_workout], [hc_fragment, unrelated])
        self.assertNotIn(hc_fragment, out)
        self.assertIn(unrelated, out)
        self.assertIn(ksfit_workout, out)


class KsFitPullFailureTests(unittest.TestCase):
    @patch("ksfit_sync.parse_ksfit_workouts", return_value=[{"id": "cached"}])
    @patch(
        "ksfit_sync.pull_ksfit_database",
        side_effect=subprocess.TimeoutExpired(["adb"], 5),
    )
    @patch(
        "health_sync._get_adb_device_serials",
        return_value=["192.168.50.72:5555"],
    )
    def test_health_sync_uses_active_serial_and_keeps_cached_workouts_on_timeout(
        self,
        _serials,
        pull,
        parse,
    ):
        from health_sync import _extract_ksfit_native_workouts

        workouts = _extract_ksfit_native_workouts()

        self.assertEqual(workouts, [{"id": "cached"}])
        self.assertEqual(pull.call_args.kwargs["serial"], "192.168.50.72:5555")
        parse.assert_called_once_with(ksfit_sync.CACHE_DB_PATH)


if __name__ == "__main__":
    unittest.main()
