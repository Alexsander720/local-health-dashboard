from __future__ import annotations

import re
from typing import Any


MEASUREMENT_FIELDS = {
    "chest_cm",
    "shoulders_cm",
    "waist_cm",
    "hips_cm",
    "biceps_cm",
    "thigh_cm",
    "calf_cm",
    "neck_cm",
    "arm_length_cm",
    "foot_cm",
}
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_legacy_flat_measurement(data: dict[str, Any]) -> bool:
    return bool(data) and set(data).issubset(MEASUREMENT_FIELDS | {"added_at"})


def normalize_measurements(
    data: Any,
    *,
    fallback_date: str | None = None,
) -> dict[str, dict[str, Any]]:
    if not isinstance(data, dict) or not data:
        return {}

    if _is_legacy_flat_measurement(data):
        if not fallback_date or not DATE_PATTERN.fullmatch(fallback_date):
            return {}
        entry = {
            key: float(value) if key in MEASUREMENT_FIELDS else value
            for key, value in data.items()
            if key == "added_at" or isinstance(value, (int, float))
        }
        return {fallback_date: entry} if entry else {}

    normalized = {}
    for date, entry in data.items():
        if not DATE_PATTERN.fullmatch(str(date)) or not isinstance(entry, dict):
            continue
        clean_entry = {
            key: value
            for key, value in entry.items()
            if key == "added_at" or (
                key in MEASUREMENT_FIELDS and isinstance(value, (int, float))
            )
        }
        if clean_entry:
            normalized[str(date)] = clean_entry
    return normalized


def validate_measurements(data: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(data, dict):
        raise ValueError("measurements must be a date-keyed object")
    if _is_legacy_flat_measurement(data):
        raise ValueError("measurements must be date-keyed")

    normalized = normalize_measurements(data)
    if len(normalized) != len(data):
        raise ValueError("measurements contain invalid dates or entries")
    return normalized
