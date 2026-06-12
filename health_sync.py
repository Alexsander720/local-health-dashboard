"""
Автоматическая синхронизация здоровья с телефона на ПК.

Тянет БД Mobvoi Health через phone_server.py по WiFi (без кабеля).
Парсит сон и все метрики, сохраняет JSON.
Запускается по расписанию через Windows Task Scheduler.

Использование:
    python health_sync.py              # синхронизировать и обновить JSON
    python health_sync.py --days 30    # за 30 дней
    python health_sync.py --install    # установить в Task Scheduler (каждый час)
    python health_sync.py --uninstall  # удалить из Task Scheduler
"""

import argparse
import concurrent.futures
import ipaddress
import json
import math
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import storage_utils

# When this script runs under pythonw (scheduled task, no console), each child
# console process (adb, schtasks, sqlite3) makes Windows pop a black console
# window that flashes for ~1s. CREATE_NO_WINDOW suppresses that flash without
# changing any behavior. _run() is a drop-in for subprocess.run.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_subprocess_run = subprocess.run


def _run(*args, **kwargs):
    if os.name == "nt":
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | _NO_WINDOW
    return _subprocess_run(*args, **kwargs)


PHONE_PORT = 8898
PHONE_IP_LAST_KNOWN = Path(__file__).parent / "sleep-data" / ".phone_ip"

# Will be set dynamically by _resolve_phone_base()
_phone_base: str | None = None


def _parse_adb_device_serials(output: str) -> list[str]:
    serials = []
    for raw in (output or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def _get_adb_device_serials() -> list[str]:
    try:
        r = _run(["adb", "devices", "-l"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return _parse_adb_device_serials(r.stdout)
    except Exception:
        pass
    return []


def _get_phone_ip_via_adb() -> str | None:
    """Ask ADB for phone's wlan0 IP."""
    serials = _get_adb_device_serials() or [None]
    for serial in serials:
        try:
            cmd = ["adb"]
            if serial:
                cmd.extend(["-s", serial])
            cmd.extend(["shell", "ip addr show wlan0"])
            r = _run(cmd, capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                import re
                m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/", r.stdout)
                if m:
                    return m.group(1)
        except Exception:
            pass
    return None


def _try_phone_at(ip: str) -> bool:
    """Check if phone server responds at given IP."""
    try:
        req = urllib.request.Request(f"http://{ip}:{PHONE_PORT}/")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            return data.get("status") == "alive"
    except Exception:
        return False


def _get_local_ipv4s() -> list[str]:
    ips = set()
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            ips.add(ip)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return sorted(ip for ip in ips if ip and not ip.startswith("127."))


def _iter_lan_scan_candidates(seed_ips: list[str]):
    seen_networks = set()
    seen_hosts = set()
    for seed in seed_ips:
        try:
            ip = ipaddress.ip_address((seed or "").strip())
        except ValueError:
            continue
        if ip.version != 4 or ip.is_loopback:
            continue
        network = ipaddress.ip_network(f"{ip}/24", strict=False)
        if network in seen_networks:
            continue
        seen_networks.add(network)
        for host in network.hosts():
            text = str(host)
            if text not in seen_hosts:
                seen_hosts.add(text)
                yield text


def _scan_lan_for_phone(seed_ips: list[str]) -> str | None:
    candidates = list(_iter_lan_scan_candidates(seed_ips))
    if not candidates:
        return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=48) as pool:
        future_to_ip = {pool.submit(_try_phone_at, ip): ip for ip in candidates}
        for future in concurrent.futures.as_completed(future_to_ip):
            ip = future_to_ip[future]
            try:
                if future.result():
                    return ip
            except Exception:
                pass
    return None


def _save_last_known_ip(ip: str):
    try:
        PHONE_IP_LAST_KNOWN.write_text(ip, encoding="utf-8")
    except Exception:
        pass


def _load_last_known_ip() -> str | None:
    try:
        if PHONE_IP_LAST_KNOWN.exists():
            ip = PHONE_IP_LAST_KNOWN.read_text(encoding="utf-8").lstrip("\ufeff").strip()
            if ip:
                return ip
    except Exception:
        pass
    return None


def _resolve_phone_base() -> str | None:
    """Auto-discover phone: ADB IP query → last known IP → localhost → adb forward."""
    global _phone_base
    if _phone_base:
        return _phone_base

    candidates = []

    # 1) Get real IP via ADB (works over USB and adb wireless)
    adb_ip = _get_phone_ip_via_adb()
    if adb_ip:
        candidates.append(("ADB wlan0", adb_ip))

    # 2) Last known working IP
    last_ip = _load_last_known_ip()
    if last_ip and last_ip != adb_ip:
        candidates.append(("last known", last_ip))

    # 3) Localhost (in case adb forward is already set up)
    candidates.append(("localhost", "127.0.0.1"))

    for label, ip in candidates:
        if _try_phone_at(ip):
            _phone_base = f"http://{ip}:{PHONE_PORT}"
            log(f"Phone reachable via {label} ({ip})")
            if ip != "127.0.0.1":
                _save_last_known_ip(ip)
            return _phone_base

    # 4) Last resort before adb forward: scan local /24 networks for phone_server.
    scan_seed_ips = [ip for _, ip in candidates if ip != "127.0.0.1"] + _get_local_ipv4s()
    found_ip = _scan_lan_for_phone(scan_seed_ips)
    if found_ip:
        _phone_base = f"http://{found_ip}:{PHONE_PORT}"
        log(f"Phone reachable via LAN scan ({found_ip})")
        _save_last_known_ip(found_ip)
        return _phone_base

    # 5) Last resort: set up ADB forward and retry localhost
    try:
        r = _run(
            ["adb", "forward", f"tcp:{PHONE_PORT}", f"tcp:{PHONE_PORT}"],
            capture_output=True, timeout=5,
        )
        if r.returncode == 0 and _try_phone_at("127.0.0.1"):
            _phone_base = f"http://127.0.0.1:{PHONE_PORT}"
            log("Phone reachable via ADB forward (auto-setup)")
            return _phone_base
    except Exception:
        pass

    return None


def _url(path: str) -> str:
    """Build URL for a phone server endpoint."""
    base = _resolve_phone_base()
    if not base:
        return f"http://127.0.0.1:{PHONE_PORT}{path}"
    return f"{base}{path}"




BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "sleep-data"
DB_PATH = DATA_DIR / "health_common.db"
ZEPP_DB_PATH = DATA_DIR / "zepp_origin.db"
YAZIO_DB_PATH = DATA_DIR / "yazio_shared.db"
YAZIO_PLAN_PATH = DATA_DIR / "yazio_plan.db"
HEALTHCONNECT_PATH = DATA_DIR / "healthconnect.db"
HOMEWORKOUT_PATH = DATA_DIR / "homeworkout.db"
GOOGLEFIT_SESSION_LOCATION_PATH = DATA_DIR / "googlefit_session_location.db"
GOOGLEFIT_MINIMAP_LOCATIONS_PATH = DATA_DIR / "googlefit_minimap_locations.db"
JSON_PATH = DATA_DIR / "latest_sync.json"
LOG_PATH = DATA_DIR / "sync.log"
MANUAL_NOTES_PATH = BASE_DIR / "manual_notes.json"
YAZIO_NOTES_ARCHIVE_PATH = BASE_DIR / "yazio_notes_archive.json"

TASK_NAME = "HealthSync_Mobvoi"

MSK = timezone(timedelta(hours=3))
KS_FIT_PACKAGE = "com.kingsmith.xiaojin"
GOOGLE_FIT_PACKAGE = "com.google.android.apps.fitness"
GOOGLEFIT_SESSION_LOCATION_ANDROID_PATH = (
    "/data/data/com.google.android.apps.fitness/files/accounts/4/"
    "SqliteKeyValueCache:SessionLocation.db"
)
GOOGLEFIT_MINIMAP_LOCATIONS_ANDROID_PATH = (
    "/data/data/com.google.android.apps.fitness/files/accounts/4/"
    "SqliteKeyValueCache:MinimapLocations.db"
)

# Data type IDs in Mobvoi health_common.db
TYPE_HR = 1
TYPE_SLEEP_STAGE = 2
TYPE_SPO2 = 28
TYPE_STRESS = 29
TYPE_RESP_RATE = 38
TYPE_TEMP = 42
TYPE_DISTANCE = 8
TYPE_CALORIES = 9
TYPE_STEPS = 10

STAGE_NAMES = {7: "awake", 8: "rem", 9: "light", 10: "deep"}


def _pb_read_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
        if shift > 70:
            break
    raise ValueError("truncated protobuf varint")


def _pb_skip_field(data: bytes, offset: int, wire_type: int) -> int:
    if wire_type == 0:
        _, offset = _pb_read_varint(data, offset)
        return offset
    if wire_type == 1:
        return offset + 8
    if wire_type == 2:
        size, offset = _pb_read_varint(data, offset)
        return offset + size
    if wire_type == 5:
        return offset + 4
    raise ValueError(f"unsupported protobuf wire type {wire_type}")


def _parse_googlefit_session_location_request(data: bytes) -> tuple[int | None, int | None]:
    fields: dict[int, int] = {}
    offset = 0
    while offset < len(data):
        tag, offset = _pb_read_varint(data, offset)
        field_no = tag >> 3
        wire_type = tag & 7
        if wire_type == 0:
            fields[field_no], offset = _pb_read_varint(data, offset)
        else:
            offset = _pb_skip_field(data, offset, wire_type)
    return fields.get(1), fields.get(2)


def _parse_googlefit_location_point(data: bytes) -> dict | None:
    import struct

    point: dict = {}
    offset = 0
    while offset < len(data):
        tag, offset = _pb_read_varint(data, offset)
        field_no = tag >> 3
        wire_type = tag & 7
        if wire_type == 1:
            if offset + 8 > len(data):
                return None
            value = struct.unpack("<d", data[offset:offset + 8])[0]
            offset += 8
            if field_no == 1:
                point["lat"] = value
            elif field_no == 2:
                point["lon"] = value
            elif field_no == 3:
                point["altitude"] = value
        elif wire_type == 5:
            if offset + 4 > len(data):
                return None
            value = struct.unpack("<f", data[offset:offset + 4])[0]
            offset += 4
            if field_no == 4:
                point["accuracy"] = value
        elif wire_type == 0:
            value, offset = _pb_read_varint(data, offset)
            if field_no == 5:
                point["ts_ms"] = value
        else:
            offset = _pb_skip_field(data, offset, wire_type)

    lat = point.get("lat")
    lon = point.get("lon")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return point


def _parse_googlefit_location_response(data: bytes) -> list[dict]:
    points = []
    offset = 0
    while offset < len(data):
        tag, offset = _pb_read_varint(data, offset)
        field_no = tag >> 3
        wire_type = tag & 7
        if field_no == 1 and wire_type == 2:
            size, offset = _pb_read_varint(data, offset)
            raw = data[offset:offset + size]
            offset += size
            point = _parse_googlefit_location_point(raw)
            if point:
                points.append(point)
        else:
            offset = _pb_skip_field(data, offset, wire_type)
    return points


def _haversine_km(a: dict, b: dict) -> float:
    lat1 = math.radians(float(a["lat"]))
    lat2 = math.radians(float(b["lat"]))
    dlat = lat2 - lat1
    dlon = math.radians(float(b["lon"]) - float(a["lon"]))
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(h)))


def _route_distance_km(points: list[dict]) -> float:
    total = 0.0
    prev = None
    for point in points or []:
        if prev:
            total += _haversine_km(prev, point)
        prev = point
    return total


def _clean_googlefit_route_points(points: list[dict]) -> list[dict]:
    cleaned = []
    for raw in sorted(points or [], key=lambda p: p.get("ts_ms") or 0):
        try:
            point = {
                "lat": round(float(raw["lat"]), 6),
                "lon": round(float(raw["lon"]), 6),
            }
        except Exception:
            continue
        if not (-90 <= point["lat"] <= 90 and -180 <= point["lon"] <= 180):
            continue
        ts_ms = raw.get("ts_ms")
        if isinstance(ts_ms, (int, float)) and ts_ms > 0:
            point["ts_ms"] = int(ts_ms)
        if cleaned:
            prev = cleaned[-1]
            distance = _haversine_km(prev, point)
            dt_h = ((point.get("ts_ms") or 0) - (prev.get("ts_ms") or 0)) / 3_600_000
            if dt_h > 0 and distance > 0.25 and distance / dt_h > 50:
                continue
            if dt_h <= 0 and distance > 2:
                continue
        cleaned.append(point)
    return cleaned


def _decimate_route_points(points: list[dict], max_points: int = 600) -> list[dict]:
    if len(points) <= max_points:
        return [{"lat": p["lat"], "lon": p["lon"]} for p in points]
    last = len(points) - 1
    out = []
    seen = set()
    for i in range(max_points):
        idx = round((i / (max_points - 1)) * last)
        if idx in seen:
            continue
        seen.add(idx)
        p = points[idx]
        out.append({"lat": p["lat"], "lon": p["lon"]})
    return out


def _parse_googlefit_session_location_cache_row(request_data: bytes, response_data: bytes) -> dict | None:
    try:
        start_ms, end_ms = _parse_googlefit_session_location_request(request_data or b"")
        if not start_ms or not end_ms:
            return None
        points = _clean_googlefit_route_points(_parse_googlefit_location_response(response_data or b""))
        if len(points) < 2:
            return None
        return {
            "start_ms": int(start_ms),
            "end_ms": int(end_ms),
            "points": points,
        }
    except Exception:
        return None


def _parse_googlefit_minimap_request(data: bytes) -> tuple[str | None, int | None, int | None]:
    label = None
    offset = 0
    while offset < len(data or b""):
        tag, offset = _pb_read_varint(data, offset)
        field_no = tag >> 3
        wire_type = tag & 7
        if field_no == 1 and wire_type == 2:
            size, offset = _pb_read_varint(data, offset)
            label = data[offset:offset + size].decode("utf-8", errors="replace")
            offset += size
        else:
            offset = _pb_skip_field(data, offset, wire_type)

    if not label:
        return None, None, None

    start_ms = None
    end_ms = None
    if "-" in label and all(part.isdigit() for part in label.split("-", 1)):
        left, right = label.split("-", 1)
        start_ms = int(left)
        end_ms = int(right)
    else:
        tail = label.rsplit(":", 1)[-1]
        if tail.isdigit():
            start_ms = int(tail)
    return label, start_ms, end_ms


def _parse_googlefit_minimap_cache_row(request_data: bytes, response_data: bytes) -> dict | None:
    try:
        label, start_ms, end_ms = _parse_googlefit_minimap_request(request_data or b"")
        points = _clean_googlefit_route_points(_parse_googlefit_location_response(response_data or b""))
        if len(points) < 2:
            return None
        if not start_ms:
            start_ms = points[0].get("ts_ms")
        if not end_ms:
            end_ms = points[-1].get("ts_ms")
        if not start_ms or not end_ms:
            return None
        return {
            "start_ms": int(start_ms),
            "end_ms": int(end_ms),
            "points": points,
            "session_id": label,
            "route_source": "Google Fit minimap",
        }
    except Exception:
        return None


def _extract_googlefit_session_routes(db_path: Path = GOOGLEFIT_SESSION_LOCATION_PATH) -> list[dict]:
    if not db_path.exists():
        return []
    routes = []
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("""
            SELECT request_data, response_data
            FROM cache_table
            WHERE length(response_data) > 0
        """).fetchall()
        conn.close()
        for request_data, response_data in rows:
            route = _parse_googlefit_session_location_cache_row(request_data, response_data)
            if route:
                routes.append(route)
    except Exception as exc:
        log(f"Google Fit route parse error: {exc}")
    routes.sort(key=lambda r: r.get("start_ms") or 0, reverse=True)
    return routes


def _extract_googlefit_minimap_routes(db_path: Path = GOOGLEFIT_MINIMAP_LOCATIONS_PATH) -> list[dict]:
    if not db_path.exists():
        return []
    routes = []
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("""
            SELECT request_data, response_data
            FROM cache_table
            WHERE length(response_data) > 0
        """).fetchall()
        conn.close()
        for request_data, response_data in rows:
            route = _parse_googlefit_minimap_cache_row(request_data, response_data)
            if route:
                routes.append(route)
    except Exception as exc:
        log(f"Google Fit minimap parse error: {exc}")
    routes.sort(key=lambda r: r.get("start_ms") or 0, reverse=True)
    return routes


def _is_googlefit_outdoor_workout(workout: dict) -> bool:
    if workout.get("source_package") != GOOGLE_FIT_PACKAGE:
        return False
    training = str(workout.get("training") or "").lower()
    title = str(workout.get("source_title") or "").lower()
    return "treadmill" not in training and "treadmill" not in title


def _attach_googlefit_routes(workouts: list[dict], routes: list[dict]) -> list[dict]:
    if not routes:
        return workouts

    for workout in workouts:
        if not _is_googlefit_outdoor_workout(workout):
            continue
        try:
            start_ms = int(workout.get("_start_ms") or 0)
            end_ms = int(workout.get("_end_ms") or 0)
        except Exception:
            continue
        if not start_ms or not end_ms:
            continue

        best_route = None
        best_score = None
        for route in routes:
            route_start = int(route.get("start_ms") or 0)
            route_end = int(route.get("end_ms") or 0)
            if not route_start or not route_end:
                continue
            start_gap = abs(route_start - start_ms)
            end_gap = abs(route_end - end_ms)
            if start_gap > 120_000 or end_gap > 120_000:
                continue
            route_points = route.get("points") or []
            workout_distance = workout.get("distance_km")
            if route.get("route_source") == "Google Fit minimap" and workout_distance:
                route_distance = _route_distance_km(route_points)
                max_reasonable = max(float(workout_distance) * 3.0, float(workout_distance) + 0.8)
                if route_distance > max_reasonable:
                    continue
            score = (start_gap + end_gap, -len(route.get("points") or []))
            if best_score is None or score < best_score:
                best_route = route
                best_score = score

        if best_route:
            clean_points = _clean_googlefit_route_points(best_route.get("points") or [])
            if len(clean_points) >= 2:
                workout["route_points"] = _decimate_route_points(clean_points)
                workout["route_point_count"] = len(clean_points)
                workout["route_source"] = best_route.get("route_source") or "Google Fit"
    return workouts


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _yazio_item_date(item: dict) -> str | None:
    date = item.get("date")
    if not date:
        return None
    return str(date)[:10]


def _choose_yazio_consumed_items(consumed: list, day_summaries: dict[str, list]) -> list:
    """Prefer YAZIO's per-day summary over raw history when both exist.

    The raw consumedItems3 list can keep entries after the user removed them in
    the app. foodDaySummary6 reflects the current day contents for synced days.
    """
    summary_dates = {date for date, items in (day_summaries or {}).items() if items}
    if not summary_dates:
        return list(consumed or [])

    out = []
    emitted_summary_dates = set()
    for item in consumed or []:
        date = _yazio_item_date(item)
        if date in summary_dates:
            if date not in emitted_summary_dates:
                out.extend(day_summaries[date])
                emitted_summary_dates.add(date)
            continue
        out.append(item)

    for date in sorted(summary_dates - emitted_summary_dates, reverse=True):
        out.extend(day_summaries[date])
    return out


def _parse_yazio_done_trainings(child_date: str, value: dict) -> list:
    workouts = []
    date_fallback = str(child_date).strip('"')
    for training in value.get("doneTrainings") or []:
        if not isinstance(training, dict):
            continue
        dt = training.get("dateTime") or f"{date_fallback} 00:00:00"
        meta = training.get("sourceMetaData") or {}
        duration = training.get("durationInMinutes") or 0
        kcal = training.get("energyBurned") or 0
        workout = {
            "id": training.get("id"),
            "date": str(dt)[:10] if dt else date_fallback,
            "datetime": dt,
            "training": training.get("training") or training.get("type") or "training",
            "duration_min": round(duration, 1),
            "kcal": round(kcal, 1),
            "distance_m": round(training.get("distance") or 0, 1),
            "steps": round(training.get("steps") or 0),
            "source": meta.get("source"),
            "gateway": meta.get("gateway"),
        }
        workouts.append(workout)
    return workouts


def _format_manual_note_entries(entries: list) -> tuple[str, list, list]:
    combined_text = []
    all_tags = set()
    manual_entries = []
    for index, entry in enumerate(entries or []):
        txt = (entry.get("text") or "").strip()
        if not txt:
            continue
        tags = list(entry.get("tags") or [])
        t = entry.get("time", "")
        combined_text.append(f"[{t}] {txt}" if t else txt)
        for tag in tags:
            all_tags.add(tag)
        manual_entries.append({
            "index": index,
            "text": txt,
            "time": t,
            "tags": tags,
            "added_at": entry.get("added_at"),
        })
    return "\n\n".join(combined_text), sorted(all_tags), manual_entries


def _merge_yazio_feelings_archive(current: list, archived: dict | None = None) -> dict:
    merged = dict(archived or {})
    for item in current or []:
        date = (item.get("date") or "").strip('"')
        if not date:
            continue
        note = item.get("note")
        tags = item.get("tags") or []
        if not note and not tags:
            continue
        merged[date] = {"date": date, "note": note, "tags": tags}
    return merged


def _load_yazio_notes_archive() -> dict:
    if not YAZIO_NOTES_ARCHIVE_PATH.exists():
        return {}
    try:
        data = json.loads(YAZIO_NOTES_ARCHIVE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_yazio_notes_archive(archive: dict):
    try:
        storage_utils.atomic_write_json(YAZIO_NOTES_ARCHIVE_PATH, archive)
    except Exception as e:
        log(f"YAZIO notes archive save error: {e}")


def save_sync_data(data: dict) -> None:
    storage_utils.atomic_write_json(JSON_PATH, data)


def phone_reachable() -> bool:
    """Quick check if phone server is up (WiFi or ADB)."""
    return _resolve_phone_base() is not None


def _download_file(url: str, dest: Path) -> bool:
    """Download a file from phone via HTTP."""
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        if len(data) < 4096:
            log(f"  {dest.name}: too small ({len(data)} bytes), skipping")
            return False
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(data)
        if dest.exists():
            dest.unlink()
        tmp.rename(dest)
        log(f"  {dest.name}: {len(data)} bytes")
        return True
    except Exception as e:
        log(f"  {dest.name}: download failed: {e}")
        return False


def _download_private_db_via_adb(android_path: str, dest: Path) -> bool:
    """Fallback for private app DBs when the phone HTTP helper is older."""
    serials = _get_adb_device_serials() or [None]
    for serial in serials:
        tmp_phone = f"/sdcard/_health_export_{os.getpid()}_{dest.name}"
        tmp_local = dest.with_suffix(".tmp")
        try:
            adb = ["adb"]
            if serial:
                adb.extend(["-s", serial])
            cp_cmd = (
                f"cp '{android_path}' '{tmp_phone}' && "
                f"chmod 644 '{tmp_phone}' 2>/dev/null; "
                f"ls -l '{tmp_phone}' 2>/dev/null"
            )
            copy = _run(
                adb + ["shell", "su", "--mount-master", "-c", cp_cmd],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if copy.returncode != 0:
                continue
            pull = _run(
                adb + ["pull", tmp_phone, str(tmp_local)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            _run(
                adb + ["shell", "rm", "-f", tmp_phone],
                capture_output=True,
                timeout=8,
            )
            if pull.returncode != 0 or not tmp_local.exists() or tmp_local.stat().st_size < 4096:
                try:
                    tmp_local.unlink()
                except Exception:
                    pass
                continue
            if dest.exists():
                dest.unlink()
            tmp_local.rename(dest)
            log(f"  {dest.name}: {dest.stat().st_size} bytes via ADB")
            return True
        except Exception as exc:
            log(f"  {dest.name}: ADB fallback failed: {exc}")
            try:
                if tmp_local.exists():
                    tmp_local.unlink()
            except Exception:
                pass
    return False


def _wake_mobvoi_via_adb() -> bool:
    """Open Mobvoi HealthActivity so it requests fresh data from the watch.

    The launcher HomeActivity only starts a Health Connect export. HealthActivity
    also sends /health/cmd_wear_push_to_phone to the connected TicWatch.
    """
    serials = _get_adb_device_serials() or [None]
    for serial in serials:
        try:
            cmd = ["adb"]
            if serial:
                cmd.extend(["-s", serial])
            cmd.extend([
                "shell",
                "su",
                "-c",
                (
                    "am start -W -n "
                    "com.mobvoi.companion.at/"
                    "com.mobvoi.health.companion.HealthActivity"
                ),
            ])
            r = _run(cmd, capture_output=True, text=True, timeout=8)
            if r.returncode == 0 and "Status: ok" in (r.stdout or ""):
                return True
        except Exception:
            pass
    return False


def download_dbs(wake_mobvoi: bool = False) -> bool:
    """Download all DBs from phone.

    wake_mobvoi=True force-launches Mobvoi Companion on the phone first to
    flush the watch buffer. Off by default — opting in via --wake-mobvoi or
    `HEALTH_SYNC_WAKE_MOBVOI=1` because constant Mobvoi launches keep the
    phone screen waking up.
    """
    DATA_DIR.mkdir(exist_ok=True)
    if wake_mobvoi:
        if _wake_mobvoi_via_adb():
            log("Mobvoi watch sync requested; waiting 8s")
            time.sleep(8)
        else:
            log("Mobvoi wake requested but ADB launch failed")
    log("Downloading databases...")
    ok_health = _download_file(_url("/health-db"), DB_PATH)
    ok_zepp = _download_file(_url("/zepp-db"), ZEPP_DB_PATH)
    ok_yazio = _download_file(_url("/yazio-db"), YAZIO_DB_PATH)
    ok_yplan = _download_file(_url("/yazio-plan-db"), YAZIO_PLAN_PATH)
    # Health Connect — централизует тренировки с часов БЕЗ задержки YAZIO
    ok_hc = _download_file(_url("/healthconnect-db"), HEALTHCONNECT_PATH)
    # Home Workouts (Leap Fitness) — даёт breakdown какие именно упражнения и сколько каждое
    ok_hw = _download_file(_url("/homeworkout-db"), HOMEWORKOUT_PATH)
    ok_gf_routes = _download_file(_url("/googlefit-session-location-db"), GOOGLEFIT_SESSION_LOCATION_PATH)
    if not ok_gf_routes:
        ok_gf_routes = _download_private_db_via_adb(
            GOOGLEFIT_SESSION_LOCATION_ANDROID_PATH,
            GOOGLEFIT_SESSION_LOCATION_PATH,
        )
    ok_gf_minimap = _download_file(_url("/googlefit-minimap-locations-db"), GOOGLEFIT_MINIMAP_LOCATIONS_PATH)
    if not ok_gf_minimap:
        ok_gf_minimap = _download_private_db_via_adb(
            GOOGLEFIT_MINIMAP_LOCATIONS_ANDROID_PATH,
            GOOGLEFIT_MINIMAP_LOCATIONS_PATH,
        )
    return ok_health or ok_zepp or ok_yazio or ok_yplan or ok_hc or ok_hw or ok_gf_routes or ok_gf_minimap


def ts_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=MSK)


def _parse_sleep_transition(raw_value: str) -> tuple[int | None, int | None]:
    try:
        parts = str(raw_value).split(",")
        return int(parts[0]), int(parts[1])
    except (IndexError, TypeError, ValueError):
        return None, None


def _infer_sleep_sessions(
    stage_rows: list[tuple[str, int, int]],
    existing_sessions: list[tuple[str, int, int]],
    since_ms: int | None = None,
) -> list[tuple[str, int, int]]:
    """Recover completed Mobvoi sleeps that are missing from data_session."""
    valid_stages = set(STAGE_NAMES)
    inferred = []
    chain = []

    for row in sorted(stage_rows, key=lambda item: item[1]):
        first_code, next_code = _parse_sleep_transition(row[0])
        if first_code in valid_stages:
            chain.append(row)
        else:
            chain = []

        if not chain or next_code != 99:
            continue

        start_ms = chain[0][1]
        end_ms = chain[-1][2]
        duration_min = (end_ms - start_ms) / 60000
        overlaps_existing = any(
            start_ms < existing_end and end_ms > existing_start
            for _, existing_start, existing_end in existing_sessions
        )
        if (
            30 <= duration_min <= 16 * 60
            and (since_ms is None or start_ms >= since_ms)
            and not overlaps_existing
        ):
            inferred.append((f"inferred:{start_ms}:{end_ms}", start_ms, end_ms))
        chain = []

    return inferred


def _summarize_sleep_stages(
    stage_rows: list[tuple[str, int, int]],
) -> tuple[dict[int, float], list[dict]]:
    """Summarize transition rows, filling intervals omitted by Mobvoi sync."""
    rows = sorted(stage_rows, key=lambda item: item[1])
    stage_mins = {code: 0.0 for code in STAGE_NAMES}
    stage_timeline = []

    def add_segment(code: int | None, start_ms: int, end_ms: int, inferred: bool = False):
        if code not in stage_mins or end_ms <= start_ms:
            return
        duration_min = (end_ms - start_ms) / 60000
        stage_mins[code] += duration_min
        item = {
            "stage": STAGE_NAMES[code],
            "start": ts_to_dt(start_ms).isoformat(timespec="seconds"),
            "end": ts_to_dt(end_ms).isoformat(timespec="seconds"),
            "min": round(duration_min, 1),
        }
        if inferred:
            item["inferred"] = True
        stage_timeline.append(item)

    for index, row in enumerate(rows):
        first_code, next_code = _parse_sleep_transition(row[0])
        add_segment(first_code, row[1], row[2])
        if index + 1 < len(rows):
            next_start_ms = rows[index + 1][1]
            if next_start_ms > row[2]:
                add_segment(next_code, row[2], next_start_ms, inferred=True)

    return stage_mins, stage_timeline


def extract_all(days: int | None = None) -> dict:
    """Extract all health data from local DB."""
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()

    since_ms = None
    if days:
        since_ms = int((datetime.now(tz=MSK) - timedelta(days=days)).timestamp() * 1000)

    # Sessions
    query = "SELECT id, time_from, time_to FROM data_session WHERE type=100 AND deleted=0"
    params = []
    if since_ms:
        query += " AND time_from >= ?"
        params.append(since_ms)
    query += " ORDER BY time_from DESC"
    c.execute(query, params)
    sessions = c.fetchall()

    stage_query = (
        'SELECT "values", time_from, time_to FROM data_point '
        'WHERE type=? AND deleted=0'
    )
    stage_params = [TYPE_SLEEP_STAGE]
    if since_ms:
        stage_query += " AND time_from >= ?"
        stage_params.append(since_ms)
    stage_query += " ORDER BY time_from"
    c.execute(stage_query, stage_params)
    inferred_sessions = _infer_sleep_sessions(c.fetchall(), sessions, since_ms)
    if inferred_sessions:
        log(f"Recovered {len(inferred_sessions)} completed sleep sessions from stage transitions")
        sessions.extend(inferred_sessions)
        sessions.sort(key=lambda item: item[1], reverse=True)

    results = []
    for sid, sf, st in sessions:
        total_min = (st - sf) / 60000
        date_start = ts_to_dt(sf)
        date_end = ts_to_dt(st)

        # Stages
        c.execute(
            'SELECT "values", time_from, time_to FROM data_point '
            'WHERE type=? AND deleted=0 AND time_from >= ? AND time_to <= ? ORDER BY time_from',
            (TYPE_SLEEP_STAGE, sf, st)
        )
        stage_mins, stage_timeline = _summarize_sleep_stages(c.fetchall())

        sleep_min = stage_mins[8] + stage_mins[9] + stage_mins[10]

        # Metrics
        def get_metric(type_id, as_float=False):
            cast = "REAL" if as_float else "INTEGER"
            c.execute(
                f'SELECT COUNT(*), MIN(CAST("values" AS {cast})), '
                f'MAX(CAST("values" AS {cast})), AVG(CAST("values" AS {cast})) '
                f'FROM data_point WHERE type=? AND time_from >= ? AND time_from <= ?',
                (type_id, sf, st)
            )
            r = c.fetchone()
            if not r or r[0] == 0:
                return None
            fmt = (lambda x: round(x, 1)) if as_float else int
            return {"count": r[0], "min": fmt(r[1]), "max": fmt(r[2]), "avg": fmt(r[3])}

        entry = {
            "date": date_start.strftime("%Y-%m-%d"),
            "weekday": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][date_start.weekday()],
            "start": date_start.isoformat(timespec="seconds"),
            "end": date_end.isoformat(timespec="seconds"),
            "bedtime": date_start.strftime("%H:%M"),
            "waketime": date_end.strftime("%H:%M"),
            "duration_min": round(total_min, 1),
            "sleep_min": round(sleep_min, 1),
            "stages": {
                "deep": {"min": round(stage_mins[10], 1), "pct": round(stage_mins[10] / total_min * 100, 1) if total_min else 0},
                "rem": {"min": round(stage_mins[8], 1), "pct": round(stage_mins[8] / total_min * 100, 1) if total_min else 0},
                "light": {"min": round(stage_mins[9], 1), "pct": round(stage_mins[9] / total_min * 100, 1) if total_min else 0},
                "awake": {"min": round(stage_mins[7], 1), "pct": round(stage_mins[7] / total_min * 100, 1) if total_min else 0},
            },
            "stage_timeline": stage_timeline,
            "heart_rate": get_metric(TYPE_HR),
            "spo2": get_metric(TYPE_SPO2),
            "stress": get_metric(TYPE_STRESS),
            "respiratory_rate": get_metric(TYPE_RESP_RATE),
            "skin_temperature": get_metric(TYPE_TEMP, as_float=True),
        }
        if str(sid).startswith("inferred:"):
            entry["inferred_from_stages"] = True
        results.append(entry)

    # Daily metrics (24h HR, stress, SpO2, steps)
    daily = []
    c.execute("SELECT MIN(time_from), MAX(time_from) FROM data_point WHERE type=1")
    row = c.fetchone()
    if row and row[0]:
        day_start = ts_to_dt(row[0]).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = ts_to_dt(row[1]).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        current = day_start
        while current < day_end:
            ds = int(current.timestamp() * 1000)
            de = int((current + timedelta(days=1)).timestamp() * 1000)

            def day_metric(type_id):
                c.execute(
                    'SELECT COUNT(*), MIN(CAST("values" AS INTEGER)), '
                    'MAX(CAST("values" AS INTEGER)), AVG(CAST("values" AS INTEGER)) '
                    'FROM data_point WHERE type=? AND time_from >= ? AND time_from <= ?',
                    (type_id, ds, de)
                )
                r = c.fetchone()
                if not r or r[0] == 0:
                    return None
                return {"count": r[0], "min": r[1], "max": r[2], "avg": int(r[3])}

            def day_sum(type_id, as_float=False):
                cast = "REAL" if as_float else "INTEGER"
                c.execute(
                    f'SELECT SUM(CAST("values" AS {cast})), COUNT(*) '
                    f'FROM data_point WHERE type=? AND time_from >= ? AND time_from <= ?',
                    (type_id, ds, de)
                )
                r = c.fetchone()
                if not r or r[1] == 0:
                    return None
                return round(r[0], 1) if as_float else int(r[0])

            hr = day_metric(TYPE_HR)
            steps = day_sum(TYPE_STEPS)
            if hr and hr["count"] > 0:
                entry = {
                    "date": current.strftime("%Y-%m-%d"),
                    "hr": hr,
                    "stress": day_metric(TYPE_STRESS),
                    "spo2": day_metric(TYPE_SPO2),
                }
                if steps:
                    distance = day_sum(TYPE_DISTANCE, as_float=True)
                    calories = day_sum(TYPE_CALORIES, as_float=True)
                    # Active minutes = segments with steps, each ~10s
                    c.execute(
                        'SELECT COUNT(*) FROM data_point '
                        'WHERE type=? AND time_from >= ? AND time_from <= ? '
                        'AND CAST("values" AS INTEGER) > 0',
                        (TYPE_STEPS, ds, de)
                    )
                    active_pts = c.fetchone()[0]
                    entry["activity"] = {
                        "steps": steps,
                        "distance_m": distance,
                        "calories": calories,
                        "active_min": round(active_pts * 10 / 60, 1),
                    }
                daily.append(entry)
            current += timedelta(days=1)

    conn.close()

    # Weight & body composition from Zepp Life
    weight_history = []
    latest_weight = None
    if ZEPP_DB_PATH.exists():
        try:
            zconn = sqlite3.connect(str(ZEPP_DB_PATH))
            zc = zconn.cursor()
            zc.execute(
                'SELECT TIMESTAMP, WEIGHT, BMI, BODY_FAT, MUSCLE, MOISTURE, '
                'BASAL_METABOLISM, VISCERAL_FAT, BONE_MASS, SCORE '
                'FROM WEIGHT_INFOS WHERE HEIGHT=178 ORDER BY TIMESTAMP DESC'
            )
            for row in zc.fetchall():
                entry = {
                    "date": ts_to_dt(row[0]).strftime("%Y-%m-%d"),
                    "time": ts_to_dt(row[0]).strftime("%H:%M"),
                    "weight_kg": round(row[1], 1),
                    "bmi": round(row[2], 1),
                }
                if row[3] is not None:  # Full body composition
                    entry.update({
                        "body_fat_pct": round(row[3], 1),
                        "muscle_pct": round(row[4], 1),
                        "moisture_pct": round(row[5], 1) if row[5] else None,
                        "basal_metabolism": int(row[6]) if row[6] else None,
                        "visceral_fat": round(row[7], 1) if row[7] else None,
                        "bone_mass_kg": round(row[8], 1) if row[8] else None,
                        "body_score": int(row[9]) if row[9] else None,
                    })
                weight_history.append(entry)
            if weight_history:
                latest_weight = weight_history[0]["weight_kg"]
            zconn.close()
        except Exception as e:
            log(f"Zepp weight error: {e}")

    # Nutrition from YAZIO
    nutrition_days = []
    yazio_goals = None
    yazio_feelings = []
    yazio_water = []
    yazio_trainings_steps = []
    yazio_workouts = []
    yazio_active_fasting = None
    if YAZIO_DB_PATH.exists():
        try:
            yconn = sqlite3.connect(str(YAZIO_DB_PATH))
            yc = yconn.cursor()

            # Load product details
            yc.execute('SELECT childKey, value FROM genericEntry WHERE rootKey="productDetail4"')
            yazio_products = {}
            for row in yc.fetchall():
                p = json.loads(row[1])
                yazio_products[p["id"]] = p

            # Load consumed items
            yc.execute('SELECT value FROM genericEntry WHERE rootKey="consumedItems3"')
            row = yc.fetchone()
            consumed = json.loads(row[0]) if row else []

            yc.execute('SELECT childKey, value FROM genericEntry WHERE rootKey="foodDaySummary6"')
            day_summaries = {}
            for child_key, value in yc.fetchall():
                try:
                    date = str(child_key).strip('"')
                    items = json.loads(value)
                    if date and isinstance(items, list):
                        day_summaries[date] = items
                except Exception:
                    pass
            consumed = _choose_yazio_consumed_items(consumed, day_summaries)

            yconn.close()

            # Goals + feelings + water + trainings from plan DB
            if YAZIO_PLAN_PATH.exists():
                pconn = sqlite3.connect(str(YAZIO_PLAN_PATH))
                pc = pconn.cursor()

                pc.execute('SELECT value FROM genericEntry WHERE rootKey="goals2" ORDER BY childKey DESC LIMIT 1')
                row = pc.fetchone()
                yazio_goals = json.loads(row[0]) if row else None

                # Feelings (notes + symptom tags)
                pc.execute('SELECT childKey, value FROM genericEntry WHERE rootKey="feelings" ORDER BY childKey DESC')
                for r in pc.fetchall():
                    try:
                        date = r[0].strip('"')
                        val = json.loads(r[1])
                        if val.get("note") or val.get("tags"):
                            yazio_feelings.append({
                                "date": date,
                                "note": val.get("note"),
                                "tags": val.get("tags", []),
                            })
                    except Exception:
                        pass

                # Water
                pc.execute('SELECT childKey, value FROM genericEntry WHERE rootKey="waterIntake" ORDER BY childKey DESC')
                for r in pc.fetchall():
                    try:
                        val = json.loads(r[1])
                        if val.get("ml", 0) > 0:
                            yazio_water.append({
                                "date": val.get("date"),
                                "ml": val.get("ml"),
                            })
                    except Exception:
                        pass

                # Step entries from doneTrainings3
                pc.execute('SELECT childKey, value FROM genericEntry WHERE rootKey="doneTrainings3" ORDER BY childKey DESC')
                for r in pc.fetchall():
                    try:
                        val = json.loads(r[1])
                        yazio_workouts.extend(_parse_yazio_done_trainings(r[0], val))
                        se = val.get("stepEntry")
                        if se and se.get("steps", 0) > 0:
                            yazio_trainings_steps.append({
                                "date": se.get("date"),
                                "steps": se.get("steps"),
                                "kcal": round(se.get("energyInKcal", 0), 1),
                                "distance_m": round(se.get("distanceInMeter", 0), 1),
                            })
                    except Exception:
                        pass

                # Active fasting
                pc.execute('SELECT value FROM genericEntry WHERE rootKey="activeFasting5"')
                row = pc.fetchone()
                if row:
                    try:
                        val = json.loads(row[0])
                        yazio_active_fasting = {
                            "plan": val.get("key"),
                            "started": val.get("start"),
                        }
                    except Exception:
                        pass

                pconn.close()

            # Aggregate by date and meal
            from collections import defaultdict
            day_data = defaultdict(lambda: {
                "kcal": 0, "protein": 0, "fat": 0, "carb": 0, "sugar": 0, "fiber": 0,
                "meals": defaultdict(lambda: {"kcal": 0, "protein": 0, "fat": 0, "carb": 0, "items": []}),
            })
            for item in consumed:
                pid = item.get("product_id") or item.get("meal_id")
                if not pid or pid not in yazio_products:
                    continue
                p = yazio_products[pid]
                nf = p.get("nutritionFacts", {})
                nuts = nf.get("nutrients", {})
                amount = item.get("amount", 0)
                date = item["date"][:10]
                meal = item.get("daytime", "other")

                kcal = nf.get("energy", 0) * amount
                protein = nuts.get("nutrient.protein", 0) * amount
                fat = nuts.get("nutrient.fat", 0) * amount
                carb = nuts.get("nutrient.carb", 0) * amount
                sugar = nuts.get("nutrient.sugar", 0) * amount
                fiber = nuts.get("nutrient.fiber", 0) * amount

                dd = day_data[date]
                dd["kcal"] += kcal
                dd["protein"] += protein
                dd["fat"] += fat
                dd["carb"] += carb
                dd["sugar"] += sugar
                dd["fiber"] += fiber

                m = dd["meals"][meal]
                m["kcal"] += kcal
                m["protein"] += protein
                m["fat"] += fat
                m["carb"] += carb
                m["items"].append({"name": p["name"], "amount_g": round(amount), "kcal": round(kcal)})

            for date in sorted(day_data.keys(), reverse=True):
                dd = day_data[date]
                if dd["kcal"] < 1:
                    continue
                meals_out = {}
                for meal_name in ["breakfast", "lunch", "dinner", "snack"]:
                    m = dd["meals"].get(meal_name)
                    if m and m["kcal"] > 0:
                        meals_out[meal_name] = {
                            "kcal": round(m["kcal"]),
                            "protein_g": round(m["protein"], 1),
                            "fat_g": round(m["fat"], 1),
                            "carb_g": round(m["carb"], 1),
                            "items": m["items"],
                        }
                nutrition_days.append({
                    "date": date,
                    "total_kcal": round(dd["kcal"]),
                    "protein_g": round(dd["protein"], 1),
                    "fat_g": round(dd["fat"], 1),
                    "carb_g": round(dd["carb"], 1),
                    "sugar_g": round(dd["sugar"], 1),
                    "fiber_g": round(dd["fiber"], 1),
                    "meals": meals_out,
                })
        except Exception as e:
            log(f"YAZIO nutrition error: {e}")

    result = {
        "synced_at": datetime.now(tz=MSK).isoformat(timespec="seconds"),
        "user": {
            "height_cm": 178,
            "weight_kg": latest_weight or 95,
            "age": 26,
            "gender": "male",
        },
        "sleep_sessions": results,
        "daily_metrics": daily,
        "weight_history": weight_history,
        "nutrition": nutrition_days,
    }
    if yazio_goals:
        result["nutrition_goals"] = {
            "kcal": round(yazio_goals.get("energy", 0)),
            "protein_g": round(yazio_goals.get("protein", 0), 1),
            "fat_g": round(yazio_goals.get("fat", 0), 1),
            "carb_g": round(yazio_goals.get("carb", 0), 1),
            "weight_goal_kg": round(yazio_goals.get("weight", 0) / 1000, 1),
            "water_l": yazio_goals.get("water", 0),
            "steps_goal": yazio_goals.get("steps", 0),
        }
    # Объединяем yazio+manual заметки в единую структуру по дате.
    # Одна дата = одна запись с yazio_note и/или manual_note (не дубли).
    by_date: dict[str, dict] = {}
    yazio_archive = _merge_yazio_feelings_archive(yazio_feelings, _load_yazio_notes_archive())
    if yazio_archive:
        _save_yazio_notes_archive(yazio_archive)

    for f in sorted(yazio_archive.values(), key=lambda x: x.get("date", ""), reverse=True):
        date = (f.get("date") or "").strip('"')
        if not date:
            continue
        by_date[date] = {
            "date": date,
            "yazio_note": f.get("note"),
            "yazio_tags": f.get("tags") or [],
            "manual_note": None,
            "manual_tags": [],
        }

    if MANUAL_NOTES_PATH.exists():
        try:
            manual = json.loads(MANUAL_NOTES_PATH.read_text(encoding="utf-8"))
            for date, entries in manual.items():
                if not entries:
                    continue
                combined_text, all_tags, manual_entries = _format_manual_note_entries(entries)
                if not combined_text:
                    continue
                entry = by_date.setdefault(date, {
                    "date": date, "yazio_note": None, "yazio_tags": [],
                    "manual_note": None, "manual_tags": [], "manual_entries": [],
                })
                entry["manual_note"] = combined_text
                entry["manual_tags"] = all_tags
                entry["manual_entries"] = manual_entries
        except Exception as e:
            log(f"Manual notes error: {e}")

    feelings_merged = sorted(by_date.values(), key=lambda x: x["date"], reverse=True)
    if feelings_merged:
        result["feelings"] = feelings_merged
    if yazio_water:
        result["water_intake"] = yazio_water
    if yazio_trainings_steps:
        result["steps_phone"] = yazio_trainings_steps

    # ===== Workouts: YAZIO (с задержкой через Google Fit) + Health Connect (свежак с часов) =====
    user_age = (result.get("user") or {}).get("age", 26)
    hc_workouts = _extract_healthconnect_workouts(user_age=user_age)
    ksfit_native = _extract_ksfit_native_workouts()
    if ksfit_native:
        hc_workouts = _prefer_ksfit_native_over_hc(ksfit_native, hc_workouts)
    all_workouts = _merge_workouts(yazio_workouts, hc_workouts)
    homeworkouts = _extract_homeworkouts()
    if homeworkouts:
        all_workouts = _merge_homeworkouts_into(all_workouts, homeworkouts)
    if all_workouts:
        result["workouts"] = all_workouts

    if yazio_active_fasting:
        result["fasting_plan"] = yazio_active_fasting
    return result


# Health Connect exercise_type → русские названия
# https://developer.android.com/reference/android/health/connect/datatypes/ExerciseSessionRecord
HC_EXERCISE_TYPES = {
    0: "other",
    2: "badminton",
    4: "baseball",
    5: "basketball",
    8: "biking",
    9: "biking_stationary",
    10: "boot_camp",
    11: "aerobics",  # boxing? нет — boxing=12
    12: "boxing",
    13: "calisthenics",
    14: "cricket",
    16: "dancing",
    25: "elliptical",
    26: "exercise_class",
    27: "fencing",
    28: "football_american",
    29: "football_australian",
    31: "frisbee_disc",
    32: "golf",
    33: "guided_breathing",
    34: "gymnastics",
    35: "handball",
    36: "high_intensity_interval",
    37: "hiking",
    38: "ice_hockey",
    39: "ice_skating",
    44: "martial_arts",
    46: "paddling",
    47: "paragliding",
    48: "pilates",
    50: "racquetball",
    51: "rock_climbing",
    52: "roller_hockey",
    53: "rowing",
    54: "rowing_machine",
    55: "rugby",
    56: "running",
    57: "running_treadmill",
    58: "sailing",
    59: "scuba_diving",
    60: "skating",
    61: "skiing",
    62: "snowboarding",
    63: "snowshoeing",
    64: "soccer",
    65: "softball",
    66: "squash",
    68: "stair_climbing",
    69: "stair_climbing_machine",
    70: "strength_training",
    71: "stretching",
    72: "surfing",
    73: "swimming_open_water",
    74: "swimming_pool",
    75: "table_tennis",
    76: "tennis",
    78: "volleyball",
    79: "walking",
    80: "water_polo",
    81: "weightlifting",
    82: "wheelchair",
    83: "yoga",
}

# Русификация для HTML
HC_TYPE_RU = {
    "aerobics": "Аэробика", "strength_training": "Силовая", "running": "Бег",
    "running_treadmill": "Бег (дорожка)", "walking_treadmill": "Ходьба на дорожке",
    "biking": "Велосипед",
    "biking_stationary": "Велотренажёр", "walking": "Ходьба",
    "swimming_pool": "Бассейн", "swimming_open_water": "Открытая вода",
    "yoga": "Йога", "pilates": "Пилатес", "stretching": "Растяжка",
    "weightlifting": "Тяжёлая атлетика", "boxing": "Бокс",
    "high_intensity_interval": "HIIT", "calisthenics": "Калистеника",
    "elliptical": "Эллипс", "rowing": "Гребля", "rowing_machine": "Гребля (тренажёр)",
    "stair_climbing_machine": "Степпер", "stair_climbing": "Лестница",
    "dancing": "Танцы", "martial_arts": "Единоборства",
    "hiking": "Поход", "tennis": "Теннис", "table_tennis": "Настольный теннис",
    "basketball": "Баскетбол", "soccer": "Футбол", "volleyball": "Волейбол",
    "exercise_class": "Групповая тренировка", "boot_camp": "Bootcamp",
    "guided_breathing": "Дыхательные практики", "other": "Тренировка",
}


def _healthconnect_training_type(
    app_package: str | None,
    app_name: str | None,
    exercise_type: int | None,
    title: str | None,
    client_record_id: str | None,
) -> str:
    type_name = HC_EXERCISE_TYPES.get(exercise_type, f"type_{exercise_type}")
    if client_record_id and "watch-activemode:" in client_record_id:
        parts = client_record_id.split(":")
        if len(parts) >= 4:
            type_name = parts[-2]

    title_norm = (title or "").strip().lower()
    app_norm = (app_package or "").strip().lower()
    name_norm = (app_name or "").strip().lower()
    is_ks_fit = app_norm == KS_FIT_PACKAGE or "ks fit" in name_norm or "kingsmith" in name_norm
    if is_ks_fit:
        if "walk" in title_norm or "ход" in title_norm:
            return "walking_treadmill"
        if "run" in title_norm or "бег" in title_norm:
            return "running_treadmill"

    return type_name


# ===== Workout load metrics (Heart Points / TRIMP / zones) =====

def _hr_zones_thresholds(hr_max: int) -> dict:
    """Стандартные 5 зон в % от max HR."""
    return {
        "z1": (0.50 * hr_max, 0.60 * hr_max),  # recovery / very light
        "z2": (0.60 * hr_max, 0.70 * hr_max),  # aerobic base
        "z3": (0.70 * hr_max, 0.80 * hr_max),  # tempo
        "z4": (0.80 * hr_max, 0.90 * hr_max),  # threshold / lactate
        "z5": (0.90 * hr_max, 1.01 * hr_max),  # VO2max / anaerobic
    }


def _compute_workout_load(hr_series: list, hr_max: int, hr_rest: int) -> dict:
    """Вычисляем Heart Points (Google), время в зонах, Edwards' TRIMP.

    hr_series — отсортированный список (timestamp_ms, bpm).
    Heart Points: 1 pt/min при HR 50-70% max (moderate), 2 pt/min при ≥ 70% (vigorous).
    Edwards TRIMP: сумма (минуты в зоне × вес зоны: z1=1, z2=2, ..., z5=5).
    """
    if not hr_series or hr_max <= 0:
        return {}

    zones = _hr_zones_thresholds(hr_max)
    zone_min = {k: 0.0 for k in zones}
    heart_points = 0.0
    trimp = 0.0
    weights = {"z1": 1, "z2": 2, "z3": 3, "z4": 4, "z5": 5}

    for i, (ts, bpm) in enumerate(hr_series):
        # Длительность от текущего sample до следующего (cap 30s — гэпы не раздуваем)
        if i + 1 < len(hr_series):
            dt_sec = (hr_series[i + 1][0] - ts) / 1000.0
            dt_sec = max(0.0, min(dt_sec, 30.0))
        else:
            dt_sec = 5.0
        minutes = dt_sec / 60.0

        # Определяем зону
        zone = None
        for zname, (lo, hi) in zones.items():
            if lo <= bpm < hi:
                zone = zname
                break
        if zone is None and bpm >= zones["z5"][1]:
            zone = "z5"
        if zone is None:
            continue  # < 50% max — отдых, не считаем

        zone_min[zone] += minutes
        # Heart Points: z1+z2 (50-70%) = 1 pt/min, z3+z4+z5 (≥70%) = 2 pt/min
        if zone in ("z1", "z2"):
            heart_points += minutes * 1
        else:
            heart_points += minutes * 2
        # Edwards TRIMP
        trimp += minutes * weights[zone]

    return {
        "zones_min": {k: round(v, 1) for k, v in zone_min.items()},
        "heart_points": round(heart_points),
        "trimp": round(trimp),
    }


def _load_resting_hr(conn) -> int:
    """Среднее resting HR за последние 14 дней (для контекста, не для расчётов)."""
    try:
        cutoff = int((datetime.now(tz=MSK) - timedelta(days=14)).timestamp() * 1000)
        avg = conn.execute(
            "SELECT AVG(beats_per_minute) FROM resting_heart_rate_record_table WHERE time >= ?",
            (cutoff,),
        ).fetchone()[0]
        return round(avg) if avg else 60
    except Exception:
        return 60


def _extract_healthconnect_workouts(user_age: int = 26) -> list:
    """Парсим тренировки из Health Connect — он получает свежак с часов раньше YAZIO.

    HC хранит энергию в milli-калориях (1 kcal = 1000 единиц).
    - active_calories_burned_record_table — короткие записи активных kcal
    - total_calories_burned_record_table — суммарные интервалы (включая BMR)
    Mobvoi Health (companion TicWatch) для walking пишет ТОЛЬКО в total.
    Для aerobics/strength — в обе. Берём ту что даст ненулевую сумму,
    приоритет active (показывает чисто тренировку).

    Шаги — из steps_record_table (sum of count в окне), дистанция — из distance_record_table.
    Heart Points + zones + TRIMP — рассчитываются из HR-серий локально.

    user_age для расчёта max HR (220-age = 194 при 26).
    """
    if not HEALTHCONNECT_PATH.exists():
        return []
    workouts = []
    try:
        conn = sqlite3.connect(str(HEALTHCONNECT_PATH))
        cur = conn.cursor()
        cur.execute("""
            SELECT e.row_id, e.client_record_id, e.start_time, e.end_time, e.start_zone_offset,
                   e.exercise_type, e.title, e.app_info_id, a.package_name, a.app_name
            FROM exercise_session_record_table e
            LEFT JOIN application_info_table a ON e.app_info_id = a.row_id
            WHERE e.end_time > e.start_time
            ORDER BY e.start_time DESC
        """)
        sessions = cur.fetchall()

        def _safe_query(sql: str) -> list:
            try:
                cur.execute(sql)
                return cur.fetchall()
            except Exception:
                return []

        # Включаем app_info_id чтобы дедуплицировать дубли источников
        # (Mobvoi и Google Fit пишут ОДНУ И ТУ ЖЕ ходьбу — суммировать = ×2)
        active_rows = _safe_query("SELECT app_info_id, start_time, end_time, energy FROM active_calories_burned_record_table")
        total_rows = _safe_query("SELECT app_info_id, start_time, end_time, energy FROM total_calories_burned_record_table")
        steps_rows = _safe_query("SELECT app_info_id, start_time, end_time, count FROM steps_record_table")
        dist_rows = _safe_query("SELECT app_info_id, start_time, end_time, distance FROM distance_record_table")
        elev_rows = _safe_query("SELECT app_info_id, start_time, end_time, elevation FROM elevation_gained_record_table")

        # HR — связка через parent_key (в series у каждой записи есть epoch_millis)
        try:
            hr_data = cur.execute("""
                SELECT h.epoch_millis, h.beats_per_minute
                FROM heart_rate_record_series_table h
                JOIN heart_rate_record_table p ON h.parent_key = p.row_id
                ORDER BY h.epoch_millis
            """).fetchall()
        except Exception:
            hr_data = []

        # Resting HR baseline + max HR из возраста (для зон/TRIMP)
        hr_rest = _load_resting_hr(conn)
        hr_max_age = max(150, 220 - int(user_age or 26))  # min 150 fallback
        googlefit_routes = _extract_googlefit_session_routes() + _extract_googlefit_minimap_routes()

        def _max_per_source(rows: list, start_ms: int, end_ms: int) -> float:
            """Группируем по app_info_id, берём максимум суммы среди источников.

            Этим мы избегаем дублирования: Mobvoi + Google Fit пишут одно и то же.
            Берём source у которого сумма ВЫШЕ (= наиболее полные данные).
            """
            by_app: dict = {}
            for app_id, r_start, r_end, val in rows:
                if r_end > start_ms and r_start < end_ms:
                    try:
                        by_app[app_id] = by_app.get(app_id, 0.0) + float(val)
                    except Exception:
                        pass
            return max(by_app.values()) if by_app else 0.0

        def _hr_in_window(start_ms: int, end_ms: int) -> list:
            """Возвращаем [(ts_ms, bpm)] отсортированный, в окне сессии."""
            return [(ts, bpm) for ts, bpm in hr_data if start_ms <= ts < end_ms and bpm]

        def _hr_stats_from_series(series: list) -> tuple:
            if not series:
                return (None, None, None)
            bpms = [b for _, b in series]
            return (round(sum(bpms) / len(bpms)), max(bpms), min(bpms))

        for row_id, crid, start_ms, end_ms, zone_offset, ex_type, title, app_info_id, app_package, app_name in sessions:
            tz = timezone(timedelta(seconds=zone_offset or 0))
            start = datetime.fromtimestamp(start_ms / 1000, tz)
            duration_min = round((end_ms - start_ms) / 60000, 1)

            # Тип: сначала из client_record_id (читабельный), потом из exercise_type кода
            type_name = _healthconnect_training_type(app_package, app_name, ex_type, title, crid)
            if duration_min < 1 and not (app_package == KS_FIT_PACKAGE and type_name == "walking_treadmill"):
                continue

            # Калории: сначала active (чистая тренировка), потом total (с BMR) если active=0
            mcal_active = _max_per_source(active_rows, start_ms, end_ms)
            mcal_total = _max_per_source(total_rows, start_ms, end_ms)
            mcal = mcal_active if mcal_active > 0 else mcal_total
            kcal = round(mcal / 1000.0, 1) if mcal > 0 else None

            # Шаги — max по источникам (избегаем дублирования Mobvoi+Google Fit)
            steps = int(_max_per_source(steps_rows, start_ms, end_ms))

            # Дистанция в метрах — тоже max по источникам
            dist_m = _max_per_source(dist_rows, start_ms, end_ms)
            dist_km = round(dist_m / 1000.0, 2) if dist_m > 0 else None

            # Elevation gain (набор высоты в метрах)
            elev_m = _max_per_source(elev_rows, start_ms, end_ms)
            elev_gain = round(elev_m, 1) if elev_m > 0 else None

            # HR во время тренировки + зоны/Heart Points/TRIMP
            hr_series = _hr_in_window(start_ms, end_ms)
            hr_avg, hr_max_obs, hr_min = _hr_stats_from_series(hr_series)
            # Реальный max HR — берём что больше: возрастной (220-age) или наблюдаемый
            hr_max_for_zones = max(hr_max_age, hr_max_obs or 0)
            load = _compute_workout_load(hr_series, hr_max_for_zones, hr_rest)

            # Pace (мин/км) — рассчитываем из дистанции + длительности
            pace_min_per_km = round(duration_min / dist_km, 1) if dist_km and dist_km > 0.1 else None
            avg_speed_kmh = round(dist_km / (duration_min / 60), 1) if dist_km and duration_min > 0 else None

            workouts.append({
                "id": f"hc_{row_id}",
                "datetime": start.strftime("%Y-%m-%d %H:%M:%S"),
                "date": start.strftime("%Y-%m-%d"),
                "training": type_name,
                "training_ru": HC_TYPE_RU.get(type_name, type_name.replace("_", " ").capitalize()),
                "source_title": title,
                "duration_min": duration_min,
                "kcal": kcal,
                "steps": steps if steps > 0 else None,
                "distance_km": dist_km,
                "elevation_gain_m": elev_gain,
                "hr_avg": hr_avg,
                "hr_max": hr_max_obs,
                "hr_min": hr_min,
                "pace_min_per_km": pace_min_per_km,
                "avg_speed_kmh": avg_speed_kmh,
                "zones_min": load.get("zones_min"),
                "heart_points": load.get("heart_points"),
                "trimp": load.get("trimp"),
                "source": "HealthConnect",
                "source_app": app_name,
                "source_package": app_package,
                "app_info_id": app_info_id,
                "_start_ms": start_ms,
                "_end_ms": end_ms,
            })
        conn.close()
        _attach_googlefit_routes(workouts, googlefit_routes)
    except Exception as e:
        log(f"Health Connect parse error: {e}")
    return workouts


def _extract_homeworkouts() -> list:
    """Парсим Home Workouts (Leap Fitness) sevenmins.db.

    Даёт уникальный breakdown — какие именно упражнения и сколько каждое.
    Health Connect видит только агрегат сессии (1 запись на тренировку),
    а здесь — per-exercise duration с учётом пауз.

    Schema: workout(_id, uid, date, info TEXT, time, temp1-3)
    info — JSON-массив сессий, каждая:
        {type:int, start:ms, end:ms, day:int, exercises:[{id, start, end, pauses}], ...}

    type/id — числовые id программы и упражнений. Имена в obfuscated APK ассетах,
    оставляем числами; AI всё равно видит durations и количество.
    """
    if not HOMEWORKOUT_PATH.exists():
        return []
    out = []
    try:
        conn = sqlite3.connect(str(HOMEWORKOUT_PATH))
        for row in conn.execute("SELECT _id, info FROM workout ORDER BY _id"):
            try:
                sessions = json.loads(row[1] or "[]")
            except Exception:
                continue
            for sess in sessions:
                start_ms = sess.get("start")
                end_ms = sess.get("end")
                exercises = sess.get("exercises") or []
                if not start_ms or not end_ms or not exercises:
                    continue

                breakdown = []
                for ex in exercises:
                    ex_id = ex.get("id")
                    ex_start = ex.get("start")
                    ex_end = ex.get("end")
                    if ex_start is None or ex_end is None:
                        continue
                    pause_ms = sum(
                        max(0, (p.get("end", 0) or 0) - (p.get("start", 0) or 0))
                        for p in (ex.get("pauses") or [])
                    )
                    dur_s = round(((ex_end - ex_start) - pause_ms) / 1000, 1)
                    if dur_s < 1:
                        continue
                    breakdown.append({"id": ex_id, "duration_s": dur_s})

                if not breakdown:
                    continue

                start = ts_to_dt(start_ms)
                duration_min = round((end_ms - start_ms) / 60000, 1)
                avg_ex_s = round(sum(b["duration_s"] for b in breakdown) / len(breakdown), 1)

                out.append({
                    "_start_ms": start_ms,
                    "datetime": start.strftime("%Y-%m-%d %H:%M:%S"),
                    "date": start.strftime("%Y-%m-%d"),
                    "homeworkout_type": sess.get("type"),
                    "homeworkout_day": sess.get("day"),
                    "duration_min": duration_min,
                    "exercises_count": len(breakdown),
                    "exercises_avg_sec": avg_ex_s,
                    "exercises_breakdown": breakdown,
                    "calories": sess.get("calories") or None,
                    "feel_level": sess.get("feelLevel") if sess.get("feelLevel", -1) != -1 else None,
                })
        conn.close()
    except Exception as e:
        log(f"Home Workouts parse error: {e}")
    return out


def _merge_homeworkouts_into(workouts: list, hw_list: list) -> list:
    """Прикрепляет homeworkout breakdown к HC/YAZIO workouts по start_time ±90s.
    Если совпадения нет — добавляет как отдельную запись (source='HomeWorkouts').
    """
    if not hw_list:
        return workouts

    used_hw = set()
    for w in workouts:
        try:
            dt_str = w.get("datetime") or ""
            w_start = datetime.fromisoformat(dt_str.replace(" ", "T")).timestamp() * 1000
        except Exception:
            continue
        for i, hw in enumerate(hw_list):
            if i in used_hw:
                continue
            if abs(hw["_start_ms"] - w_start) < 90_000:
                w["homeworkout_type"] = hw["homeworkout_type"]
                w["homeworkout_day"] = hw["homeworkout_day"]
                w["exercises_count"] = hw["exercises_count"]
                w["exercises_avg_sec"] = hw["exercises_avg_sec"]
                w["exercises_breakdown"] = hw["exercises_breakdown"]
                if not w.get("kcal") and hw.get("calories"):
                    w["kcal"] = hw["calories"]
                src = w.get("source") or ""
                if "HomeWorkouts" not in src:
                    w["source"] = (src + "+HomeWorkouts") if src else "HomeWorkouts"
                # HC может выдать неверный exercise_type (например "sailing" для домашней тренировки)
                # — переопределяем на «домашняя тренировка», сохраняя длительность/HR/zones из HC
                w["training"] = "home_workout"
                w["training_ru"] = f"Тренировка дома (программа #{hw['homeworkout_type']}, день {hw['homeworkout_day']})"
                used_hw.add(i)
                break

    for i, hw in enumerate(hw_list):
        if i in used_hw:
            continue
        clean = dict(hw)
        clean.pop("_start_ms", None)
        clean["source"] = "HomeWorkouts"
        clean["training"] = "home_workout"
        clean["training_ru"] = "Тренировка дома"
        workouts.append(clean)

    workouts.sort(key=lambda x: x.get("datetime") or "", reverse=True)
    return workouts


def _sum_present(group: list[dict], key: str, digits: int = 1):
    values = [w.get(key) for w in group if w.get(key) is not None]
    if not values:
        return None
    return round(sum(values), digits)


def _sum_steps(group: list[dict]) -> int | None:
    values = [w.get("steps") for w in group if w.get("steps") is not None]
    if not values:
        return None
    return int(round(sum(values)))


def _merge_zones(group: list[dict]) -> dict | None:
    out = {}
    for workout in group:
        for zone, minutes in (workout.get("zones_min") or {}).items():
            if minutes:
                out[zone] = out.get(zone, 0.0) + minutes
    return {k: round(v, 1) for k, v in out.items()} if out else None


def _weighted_hr_avg(group: list[dict]) -> int | None:
    total_weight = 0.0
    weighted = 0.0
    for workout in group:
        hr = workout.get("hr_avg")
        duration = workout.get("duration_min") or 0
        if hr and duration:
            weighted += hr * duration
            total_weight += duration
    return round(weighted / total_weight) if total_weight else None


def _merge_fragmented_hc_workouts(hc_list: list) -> list:
    """KS Fit writes one treadmill walk as several adjacent HC sessions."""
    if not hc_list:
        return []

    def is_mergeable(workout: dict) -> bool:
        return (
            workout.get("source_package") == KS_FIT_PACKAGE
            and workout.get("training") == "walking_treadmill"
            and workout.get("_start_ms") is not None
            and workout.get("_end_ms") is not None
        )

    def can_join(prev: dict, cur: dict) -> bool:
        if not is_mergeable(prev) or not is_mergeable(cur):
            return False
        gap_ms = (cur.get("_start_ms") or 0) - (prev.get("_end_ms") or 0)
        return 0 <= gap_ms <= 120_000

    def finish(group: list[dict]) -> list[dict]:
        if len(group) == 1:
            return [group[0]]

        merged = dict(group[0])
        duration_min = _sum_present(group, "duration_min") or 0
        distance_km = _sum_present(group, "distance_km", digits=2)
        merged["id"] = "+".join(str(w.get("id") or "") for w in group if w.get("id"))
        merged["duration_min"] = duration_min
        merged["kcal"] = _sum_present(group, "kcal")
        merged["steps"] = _sum_steps(group)
        merged["distance_km"] = distance_km
        merged["elevation_gain_m"] = _sum_present(group, "elevation_gain_m")
        merged["hr_avg"] = _weighted_hr_avg(group)
        merged["hr_max"] = max((w.get("hr_max") for w in group if w.get("hr_max") is not None), default=None)
        merged["hr_min"] = min((w.get("hr_min") for w in group if w.get("hr_min") is not None), default=None)
        merged["zones_min"] = _merge_zones(group)
        merged["heart_points"] = _sum_present(group, "heart_points")
        merged["trimp"] = _sum_present(group, "trimp")
        merged["fragment_count"] = len(group)
        merged["_start_ms"] = group[0].get("_start_ms")
        merged["_end_ms"] = group[-1].get("_end_ms")
        merged["pace_min_per_km"] = round(duration_min / distance_km, 1) if distance_km and distance_km > 0.1 else None
        merged["avg_speed_kmh"] = round(distance_km / (duration_min / 60), 1) if distance_km and duration_min > 0 else None
        return [merged]

    ordered = sorted(hc_list, key=lambda w: w.get("_start_ms") or 0)
    merged_out = []
    group = []
    for workout in ordered:
        if group and can_join(group[-1], workout):
            group.append(workout)
            continue
        if group:
            merged_out.extend(finish(group))
        group = [workout]
    if group:
        merged_out.extend(finish(group))

    merged_out.sort(key=lambda x: x.get("datetime") or "", reverse=True)
    return merged_out


def _extract_ksfit_native_workouts() -> list:
    """Pull KS Fit native SQLite via ADB+root and parse high-resolution workouts."""
    try:
        from ksfit_sync import (
            CACHE_DB_PATH,
            DEFAULT_ADB,
            DEFAULT_SERIAL,
            parse_ksfit_workouts,
            pull_ksfit_database,
        )
    except Exception as exc:
        log(f"KS Fit sync: import failed ({exc})")
        return []

    serials = _get_adb_device_serials()
    serial = serials[0] if serials else DEFAULT_SERIAL
    try:
        pulled = pull_ksfit_database(adb=DEFAULT_ADB, serial=serial)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"KS Fit sync: pull failed ({exc}), using cached db if any")
        pulled = None
    else:
        if pulled is None:
            log("KS Fit sync: pull failed (phone offline or root denied), using cached db if any")

    try:
        workouts = parse_ksfit_workouts(CACHE_DB_PATH)
    except Exception as exc:
        log(f"KS Fit sync: parse failed ({exc})")
        return []
    if workouts:
        log(f"KS Fit sync: parsed {len(workouts)} native workouts")
    return workouts


def _prefer_ksfit_native_over_hc(ksfit_list: list, hc_list: list) -> list:
    """Replace KS Fit walking_treadmill Health Connect rows with native records.

    Health Connect splits a single KS Fit walk into 8-10 fragments and drops
    per-tick telemetry. When a native KS Fit record overlaps an HC fragment
    from the same package, swap them so downstream code sees one complete
    workout with series data. HC rows from other apps are untouched.
    """
    if not ksfit_list:
        return hc_list

    keep_hc = []
    for hc in hc_list:
        if hc.get("source_package") != KS_FIT_PACKAGE:
            keep_hc.append(hc)
            continue
        hc_start = hc.get("_start_ms") or 0
        hc_end = hc.get("_end_ms") or 0
        if any(
            (k.get("_start_ms") or 0) - 5 * 60_000 <= hc_start
            and hc_end <= (k.get("_end_ms") or 0) + 5 * 60_000
            for k in ksfit_list
        ):
            continue  # native record covers this HC fragment
        keep_hc.append(hc)

    return keep_hc + list(ksfit_list)


def _merge_workouts(yazio_list: list, hc_list: list) -> list:
    """Сливаем тренировки из YAZIO и Health Connect, дедуплицируя по start_time ±90s."""
    hc_list = _merge_fragmented_hc_workouts(hc_list)
    out = []
    used_hc = set()

    # YAZIO записи могут не иметь _start_ms — парсим datetime
    for y in yazio_list:
        try:
            dt_str = y.get("datetime") or ""
            y_start = datetime.fromisoformat(dt_str.replace(" ", "T")).timestamp() * 1000
        except Exception:
            y_start = None

        # Если в HC есть запись в окне ±90s — берём HC (с типом, дюрацией) + YAZIO калории если HC пустые
        match_hc = None
        if y_start:
            for i, h in enumerate(hc_list):
                if i in used_hc:
                    continue
                if abs(h["_start_ms"] - y_start) < 90_000:
                    match_hc = (i, h)
                    break

        if match_hc:
            i, h = match_hc
            used_hc.add(i)
            merged = dict(h)
            # Если HC калории пустые — берём YAZIO/GoogleFit
            if not merged.get("kcal"):
                merged["kcal"] = y.get("kcal")
            merged["source"] = f"HealthConnect+{y.get('source') or 'YAZIO'}"
            merged.pop("_start_ms", None)
            merged.pop("_end_ms", None)
            out.append(merged)
        else:
            out.append(y)

    # HC записи которых нет в YAZIO — добавляем
    for i, h in enumerate(hc_list):
        if i in used_hc:
            continue
        clean = dict(h)
        clean.pop("_start_ms", None)
        clean.pop("_end_ms", None)
        out.append(clean)

    out.sort(key=lambda x: x.get("datetime") or "", reverse=True)
    return out


def install_task():
    """Install Windows Scheduled Task to run sync every hour."""
    script = str(Path(__file__).resolve())
    python = sys.executable

    # Run every hour while logged in
    cmd = [
        "schtasks", "/Create", "/TN", TASK_NAME, "/F",
        "/SC", "HOURLY", "/MO", "1",
        "/TR", f'"{python}" "{script}" --days 14',
        "/RL", "HIGHEST",
    ]
    result = _run(cmd, capture_output=True)
    if result.returncode == 0:
        print(f"Task '{TASK_NAME}' installed (runs every hour)")
    else:
        err = result.stderr.decode("utf-8", errors="replace")
        print(f"Failed to install task: {err}")
        print("Try running as administrator")


def uninstall_task():
    """Remove scheduled task."""
    result = _run(
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"Task '{TASK_NAME}' removed")
    else:
        print(f"Task not found or error: {result.stderr}")


def main():
    parser = argparse.ArgumentParser(description="Health data auto-sync")
    parser.add_argument("--days", type=int, default=14, help="Days to extract (default 14)")
    parser.add_argument("--install", action="store_true", help="Install scheduled task")
    parser.add_argument("--uninstall", action="store_true", help="Remove scheduled task")
    parser.add_argument("--force", action="store_true", help="Sync even if phone unreachable")
    parser.add_argument(
        "--wake-mobvoi",
        action="store_true",
        help="Force-launch Mobvoi Companion on phone before download (flushes watch buffer). Off by default — keeps phone screen quiet on scheduled syncs.",
    )
    args = parser.parse_args()
    wake_mobvoi = args.wake_mobvoi or os.environ.get("HEALTH_SYNC_WAKE_MOBVOI") in ("1", "true", "yes", "on")

    if args.install:
        install_task()
        return
    if args.uninstall:
        uninstall_task()
        return

    log("=== Health sync started ===")

    # Check phone
    phone_online = False
    used_cached_dbs = False
    if not phone_reachable():
        log("Phone not reachable, skipping sync")
        if not args.force and not DB_PATH.exists():
            return
        log("Using cached DBs")
        used_cached_dbs = True
    else:
        phone_online = True
        if not download_dbs(wake_mobvoi=wake_mobvoi):
            if not DB_PATH.exists():
                log("No DB available, exiting")
                return
            used_cached_dbs = True

    # Extract and save
    log("Extracting data...")
    data = extract_all(days=args.days)
    data["sync_status"] = {
        "phone_online": phone_online,
        "used_cached_dbs": used_cached_dbs,
        "phone_base": _phone_base,
    }
    save_sync_data(data)

    n_sleep = len(data["sleep_sessions"])
    n_daily = len(data["daily_metrics"])
    n_weight = len(data.get("weight_history", []))
    n_nutr = len(data.get("nutrition", []))
    log(f"Done: {n_sleep} sleep, {n_daily} daily, {n_weight} weight, {n_nutr} nutrition -> {JSON_PATH.name}")

    # Print last sleep summary
    if data["sleep_sessions"]:
        s = data["sleep_sessions"][0]
        st = s["stages"]
        log(f"Last sleep: {s['date']} {s['bedtime']}-{s['waketime']} "
            f"({s['duration_min']:.0f}m) deep={st['deep']['pct']}% "
            f"rem={st['rem']['pct']}% HR={s['heart_rate']['avg'] if s['heart_rate'] else '?'}")


if __name__ == "__main__":
    main()
