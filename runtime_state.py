from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Callable


class JobRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def run(self, name: str, callback: Callable[[], Any]) -> dict[str, Any]:
        with self._lock:
            current = self._jobs.get(name)
            if current and current.get("status") == "running":
                return {"accepted": False, "job": dict(current)}
            self._jobs[name] = {
                "name": name,
                "status": "running",
                "started_at": self._now(),
                "finished_at": None,
                "last_ok": None,
                "last_error": None,
            }

        try:
            result = callback()
        except Exception as exc:
            with self._lock:
                state = self._jobs[name]
                state.update({
                    "status": "idle",
                    "finished_at": self._now(),
                    "last_ok": False,
                    "last_error": str(exc),
                })
            raise

        with self._lock:
            state = self._jobs[name]
            state.update({
                "status": "idle",
                "finished_at": self._now(),
                "last_ok": bool(result.get("ok", True)) if isinstance(result, dict) else True,
                "last_error": (
                    result.get("error")
                    if isinstance(result, dict) and not result.get("ok", True)
                    else None
                ),
            })
            finished = dict(state)
        return {"accepted": True, "result": result, "job": finished}

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {name: dict(state) for name, state in self._jobs.items()}


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _record_time(record: dict[str, Any]) -> datetime | None:
    for key in ("end", "datetime", "start", "date"):
        parsed = _parse_datetime(record.get(key))
        if parsed is not None:
            if key == "date" and record.get("time"):
                combined = _parse_datetime(f"{record['date']}T{record['time']}")
                return combined or parsed
            return parsed
    return None


def _source_entry(
    label: str,
    records: list[dict[str, Any]],
    *,
    now: datetime,
    stale_after_hours: int,
    cached: bool = False,
    remote: bool = True,
) -> dict[str, Any]:
    timestamps = [stamp for stamp in (_record_time(record) for record in records) if stamp]
    latest = max(timestamps) if timestamps else None
    age_hours = max(0.0, (now - latest).total_seconds() / 3600) if latest else None

    if not records:
        state = "missing"
    elif cached and remote:
        state = "cached"
    elif age_hours is None or age_hours > stale_after_hours:
        state = "stale"
    else:
        state = "fresh"

    return {
        "label": label,
        "state": state,
        "records": len(records),
        "last_record_at": latest.isoformat(timespec="seconds") if latest else None,
        "age_hours": round(age_hours, 1) if age_hours is not None else None,
        "stale_after_hours": stale_after_hours,
    }


def build_source_status(
    data: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    now = now or datetime.now().astimezone()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)

    sync_status = data.get("sync_status") or {}
    cached = bool(sync_status.get("used_cached_dbs"))
    sleep = list(data.get("sleep_sessions") or [])
    daily = list(data.get("daily_metrics") or [])
    weights = list(data.get("weight_history") or [])
    nutrition = list(data.get("nutrition") or [])
    feelings = list(data.get("feelings") or [])
    workouts = list(data.get("workouts") or [])

    yazio_notes = [item for item in feelings if item.get("yazio_note") or item.get("yazio_tags")]
    manual_notes = [
        item for item in feelings
        if item.get("manual_note") or item.get("manual_entries") or item.get("manual_tags")
    ]
    health_connect = [
        item for item in workouts
        if "healthconnect" in str(item.get("source") or "").replace(" ", "").lower()
    ]
    google_fit = [
        item for item in workouts
        if "google fit" in str(item.get("source_app") or "").lower()
        or "googlefit" in str(item.get("source") or "").replace(" ", "").lower()
    ]
    ks_fit = [
        item for item in workouts
        if "ks fit" in str(item.get("source_app") or "").lower()
        or "ksfit" in str(item.get("source") or "").replace("_", "").lower()
    ]

    return {
        "mobvoi": _source_entry(
            "Mobvoi Health",
            sleep + daily,
            now=now,
            stale_after_hours=48,
            cached=cached,
        ),
        "zepp_scale": _source_entry(
            "Zepp / весы",
            weights,
            now=now,
            stale_after_hours=14 * 24,
            cached=cached,
        ),
        "yazio": _source_entry(
            "YAZIO",
            nutrition + yazio_notes,
            now=now,
            stale_after_hours=72,
            cached=cached,
        ),
        "health_connect": _source_entry(
            "Health Connect",
            health_connect,
            now=now,
            stale_after_hours=7 * 24,
            cached=cached,
        ),
        "google_fit": _source_entry(
            "Google Fit",
            google_fit,
            now=now,
            stale_after_hours=7 * 24,
            cached=cached,
        ),
        "ks_fit": _source_entry(
            "KS Fit",
            ks_fit,
            now=now,
            stale_after_hours=30 * 24,
            cached=cached,
        ),
        "manual": _source_entry(
            "Локальные заметки",
            manual_notes,
            now=now,
            stale_after_hours=30 * 24,
            cached=False,
            remote=False,
        ),
    }
