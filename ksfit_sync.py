"""KS Fit (Kingsmith treadmill) native SQLite sync.

Pulls /data/data/com.kingsmith.xiaojin/app_flutter/<uid>_ex (SQLite) over
adb+root from the phone, then parses workouts with high-resolution telemetry.

Why bother when Health Connect already exposes KS Fit walks?
  Health Connect strips per-tick speed / cadence / step series and even
  fragments one walk into 8-10 micro-sessions. The native SQLite keeps:
    - sport_report_point (~2 s sampling, speed/SPM/HR/distance)
    - record.point_list (BLE stream snapshot, varying interval)

Public API:
    pull_ksfit_database(...) -> Path | None
    parse_ksfit_workouts(db_path) -> list[dict]
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

import storage_utils
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "ksfit"
CACHE_DB_PATH = CACHE_DIR / "ksfit_storage.db"
ARCHIVE_PATH = CACHE_DIR / "workouts_archive.json"

KS_FIT_PACKAGE = "com.kingsmith.xiaojin"
APP_FLUTTER_DIR = f"/data/data/{KS_FIT_PACKAGE}/app_flutter"
DEFAULT_ADB = Path(os.environ.get("KSFIT_ADB", "adb"))
DEFAULT_SERIAL = os.environ.get("KSFIT_ADB_SERIAL", "")
MSK = timezone(timedelta(hours=3))


def _adb(adb: Path, serial: str, *args: str, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(adb), "-s", serial, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _find_storage_db(adb: Path, serial: str) -> str | None:
    """Locate the active GetStorage SQLite (<uid>_ex) on the device."""
    res = _adb(adb, serial, "shell", f"su -c 'ls -1 {APP_FLUTTER_DIR}'", timeout=8)
    if res.returncode != 0:
        return None
    candidates = [
        ln.strip()
        for ln in res.stdout.splitlines()
        if ln.strip().endswith("_ex") and "-journal" not in ln
    ]
    if not candidates:
        return None
    candidates.sort()
    return f"{APP_FLUTTER_DIR}/{candidates[-1]}"


def pull_ksfit_database(
    adb: Path = DEFAULT_ADB,
    serial: str = DEFAULT_SERIAL,
    dest: Path = CACHE_DB_PATH,
) -> Path | None:
    """Copy the KS Fit Flutter SQLite to local cache. Returns dest or None."""
    if not serial or (not adb.exists() and shutil.which(str(adb)) is None):
        return None

    if ":" in serial:
        _adb(adb, serial, "connect", serial, timeout=5)

    remote = _find_storage_db(adb, serial)
    if not remote:
        return None

    staging = f"/sdcard/ksfit_storage_{int(datetime.now().timestamp())}.db"
    copy = _adb(
        adb,
        serial,
        "shell",
        f"su -c 'cp {remote} {staging} && chmod 666 {staging}'",
        timeout=10,
    )
    if copy.returncode != 0:
        return None

    dest.parent.mkdir(parents=True, exist_ok=True)
    pull = _adb(adb, serial, "pull", staging, str(dest), timeout=20)
    _adb(adb, serial, "shell", f"rm -f {staging}", timeout=5)
    if pull.returncode != 0:
        return None
    return dest


def _parse_start_time(value: str | None) -> tuple[datetime | None, int | None]:
    if not value:
        return None, None
    try:
        dt = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK)
    except ValueError:
        return None, None
    return dt, int(dt.timestamp() * 1000)


def _parse_point_list(raw: str | None) -> list[dict]:
    """Convert record.point_list JSON-string into a clean per-tick list.

    Inferred BLE row layout (17 fields):
      [0]  some instant metric (unit unclear) — kept as raw
      [1]  cumulative steps
      [2]  ?
      [3]  cumulative distance, meters
      [4]  ?
      [5]  cadence, steps per minute
      [6]  runId
      [13] runId duplicate
      [14] event timestamp, ms
      [15] device MAC
      [16] device model
    """
    if not raw:
        return []
    try:
        rows = json.loads(raw)
    except (TypeError, ValueError):
        return []

    out: list[dict] = []
    base_ts: int | None = None
    for row in rows:
        if not isinstance(row, list) or len(row) < 15:
            continue
        try:
            ts_ms = int(row[14])
            steps_cum = int(row[1])
            dist_m_cum = int(row[3])
            spm = int(row[5])
        except (TypeError, ValueError):
            continue
        if base_ts is None:
            base_ts = ts_ms
        out.append({
            "t_s": round((ts_ms - base_ts) / 1000, 1),
            "ts_ms": ts_ms,
            "steps_cum": steps_cum,
            "dist_m_cum": dist_m_cum,
            "spm": spm,
        })
    return out


def _sport_report_points(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    """High-resolution series from sport_report_point (~2s sampling)."""
    cur = conn.execute(
        """SELECT pointTimeStamp, calorie, time, speed, step, dist, spm, heart
            FROM sport_report_point WHERE recordId=? ORDER BY pointTimeStamp""",
        (run_id,),
    )
    rows = cur.fetchall()
    if not rows:
        return []

    base_ts = rows[0][0]
    out = []
    for ts_ms, kcal_cum, t_s, speed_raw, step_cum, dist_m_cum, spm, hr in rows:
        out.append({
            "t_s": round((ts_ms - base_ts) / 1000, 1),
            "ts_ms": ts_ms,
            "kcal_cum": round(float(kcal_cum or 0), 2),
            "speed_kmh": round(float(speed_raw or 0) / 10.0, 2),
            "steps_cum": int(step_cum or 0),
            "dist_m_cum": int(dist_m_cum or 0),
            "spm": int(spm or 0),
            "hr": int(hr or 0) if hr else None,
        })
    return out


def _summarize_series(series: list[dict]) -> dict:
    if not series:
        return {}
    speeds = [s["speed_kmh"] for s in series if s.get("speed_kmh", 0) > 0]
    spms = [s["spm"] for s in series if s.get("spm")]
    hrs = [s["hr"] for s in series if s.get("hr")]
    out: dict[str, Any] = {}
    if speeds:
        out["speed_max_kmh"] = round(max(speeds), 2)
        out["speed_avg_kmh"] = round(sum(speeds) / len(speeds), 2)
    if spms:
        out["spm_max"] = max(spms)
        out["spm_avg"] = round(sum(spms) / len(spms))
    if hrs:
        out["hr_max_native"] = max(hrs)
        out["hr_avg_native"] = round(sum(hrs) / len(hrs))
    return out


def _training_name_for(model: str | None, avg_speed_kmh: float | None) -> str:
    if avg_speed_kmh is None:
        return "walking_treadmill"
    return "running_treadmill" if avg_speed_kmh >= 6.0 else "walking_treadmill"


def _load_archive(path: Path = ARCHIVE_PATH) -> dict[int, dict]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = raw.get("workouts") if isinstance(raw, dict) else raw
    by_run: dict[int, dict] = {}
    for w in items or []:
        run_id = w.get("run_id")
        if isinstance(run_id, int):
            by_run[run_id] = w
    return by_run


def _save_archive(workouts: dict[int, dict], path: Path = ARCHIVE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(workouts.values(), key=lambda w: w.get("_start_ms") or 0)
    payload = {
        "updated": datetime.now().isoformat(timespec="seconds"),
        "count": len(ordered),
        "workouts": ordered,
    }
    storage_utils.atomic_write_json(path, payload)


def parse_ksfit_workouts(
    db_path: Path = CACHE_DB_PATH,
    archive_path: Path = ARCHIVE_PATH,
) -> list[dict]:
    """Return workouts in the same shape health_sync.py emits, plus ksfit_series.

    KS Fit purges the on-device SQLite once records sync to the cloud (typically
    only the last 1-2 walks survive). We merge fresh records into a persistent
    archive so the dashboard keeps the full history.
    """
    archive = _load_archive(archive_path)
    if not db_path.exists():
        return sorted(archive.values(), key=lambda w: w.get("_start_ms") or 0, reverse=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    out: list[dict] = []
    try:
        rows = conn.execute(
            """SELECT _id, runId, detailid, start_time, steps, time, dist, consume,
                       point_list, deviceType, model, mac, device_name
                FROM record ORDER BY start_time"""
        ).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return []

    for row in rows:
        try:
            run_id = int(row["runId"])
        except (TypeError, ValueError):
            continue
        start_dt, start_ms = _parse_start_time(row["start_time"])
        if start_dt is None or start_ms is None:
            continue
        duration_s = int(row["time"] or 0)
        if duration_s < 1:
            continue
        end_ms = start_ms + duration_s * 1000

        steps = int(row["steps"] or 0) or None
        dist_m = int(row["dist"] or 0)
        dist_km = round(dist_m / 1000.0, 3) if dist_m > 0 else None
        consume = int(row["consume"] or 0)
        kcal = round(consume / 1000.0, 2) if consume > 0 else None

        duration_min = round(duration_s / 60.0, 1)
        avg_speed_kmh = None
        if dist_km and duration_min > 0:
            avg_speed_kmh = round(dist_km / (duration_min / 60), 2)
        pace_min_per_km = None
        if dist_km and dist_km >= 0.1:
            pace_min_per_km = round(duration_min / dist_km, 1)

        report_points = _sport_report_points(conn, run_id)
        point_list = _parse_point_list(row["point_list"])

        # Prefer sport_report_point series (denser sampling and has speed/HR).
        series = report_points or [
            {**p, "speed_kmh": None, "hr": None, "kcal_cum": None}
            for p in point_list
        ]
        summary_extras = _summarize_series(report_points)
        type_name = _training_name_for(
            row["model"], summary_extras.get("speed_avg_kmh") or avg_speed_kmh
        )

        workout = {
            "id": f"ksfit_{run_id}",
            "datetime": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "date": start_dt.strftime("%Y-%m-%d"),
            "training": type_name,
            "training_ru": "Ходьба на дорожке" if type_name == "walking_treadmill" else "Бег (дорожка)",
            "source_title": row["device_name"] or row["model"] or "Kingsmith",
            "duration_min": duration_min,
            "kcal": kcal,
            "steps": steps,
            "distance_km": dist_km,
            "elevation_gain_m": None,
            "pace_min_per_km": pace_min_per_km,
            "avg_speed_kmh": avg_speed_kmh,
            "source": "ksfit_native",
            "source_app": "KS Fit",
            "source_package": KS_FIT_PACKAGE,
            "device_model": row["model"],
            "device_mac": row["mac"],
            "run_id": run_id,
            "detail_id": row["detailid"],
            "_start_ms": start_ms,
            "_end_ms": end_ms,
            "ksfit_series": series,
            "ksfit_series_source": "sport_report_point" if report_points else "point_list",
        }
        workout.update(summary_extras)
        out.append(workout)

    conn.close()

    fresh_by_run = {w["run_id"]: w for w in out if w.get("run_id")}
    for run_id, workout in fresh_by_run.items():
        # Always prefer the fresher copy — it may carry extra series points
        # captured after the last sync (KS Fit appends sport_report_point rows
        # for the active walk until the user closes the session).
        archive[run_id] = workout
    _save_archive(archive, archive_path)

    merged = list(archive.values())
    merged.sort(key=lambda w: w.get("_start_ms") or 0, reverse=True)
    return merged


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sync KS Fit native workouts")
    parser.add_argument("--no-pull", action="store_true", help="parse cached db only")
    parser.add_argument("--adb", default=str(DEFAULT_ADB))
    parser.add_argument("--serial", default=DEFAULT_SERIAL)
    parser.add_argument("--db", default=str(CACHE_DB_PATH))
    args = parser.parse_args()

    db = Path(args.db)
    if not args.no_pull:
        pulled = pull_ksfit_database(adb=Path(args.adb), serial=args.serial, dest=db)
        if pulled is None:
            print("pull failed: phone not reachable or KS Fit data not exported")
        else:
            print(f"pulled: {pulled}")

    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    workouts = parse_ksfit_workouts(db)
    print(f"workouts: {len(workouts)}")
    for w in workouts[-5:]:
        series = w.get("ksfit_series") or []
        avg_speed = w.get("speed_avg_kmh") or w.get("avg_speed_kmh")
        print(
            f"  {w['datetime']} {w['training_ru']} "
            f"{w['duration_min']}min {w['distance_km']}km {w['steps']}st "
            f"avg_speed={avg_speed}km/h "
            f"series={len(series)} ({w['ksfit_series_source']})"
        )
