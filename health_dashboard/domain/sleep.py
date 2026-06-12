from __future__ import annotations

import math
from statistics import median


def _stage_minutes(session: dict, stage: str) -> float:
    return float((((session.get("stages") or {}).get(stage) or {}).get("min")) or 0)


def _actual_sleep_minutes(session: dict) -> float:
    reported = float(session.get("sleep_min") or 0)
    if reported > 0:
        return reported
    staged = sum(_stage_minutes(session, stage) for stage in ("deep", "rem", "light"))
    if staged > 0:
        return staged
    duration = float(session.get("duration_min") or 0)
    return max(0.0, duration - _stage_minutes(session, "awake"))


def aggregate_sleep_by_date(sessions: list[dict]) -> list[dict]:
    """Merge split sleep sessions from the same calendar date into one night."""
    grouped: dict[str, dict] = {}
    for session in sessions:
        date = session.get("date")
        if not date:
            continue
        entry = grouped.setdefault(
            date,
            {
                "date": date,
                "bedtime": session.get("bedtime") or "",
                "waketime": session.get("waketime") or "",
                "duration_min": 0.0,
                "sleep_min": 0.0,
                "stages": {
                    "deep": {"min": 0.0, "pct": 0},
                    "rem": {"min": 0.0, "pct": 0},
                    "light": {"min": 0.0, "pct": 0},
                    "awake": {"min": 0.0, "pct": 0},
                },
                "_hr_weighted": 0.0,
                "_hr_minutes": 0.0,
                "_inferred_segments": 0,
                "_segments": 0,
            },
        )
        duration = float(session.get("duration_min") or 0)
        actual_sleep = _actual_sleep_minutes(session)
        entry["duration_min"] += duration
        entry["sleep_min"] += actual_sleep
        entry["_segments"] += 1
        if session.get("inferred_from_stages"):
            entry["_inferred_segments"] += 1

        if session.get("bedtime"):
            entry["bedtime"] = min(
                value for value in (entry.get("bedtime"), session.get("bedtime")) if value
            )
        if session.get("waketime"):
            entry["waketime"] = max(
                value for value in (entry.get("waketime"), session.get("waketime")) if value
            )

        for stage in ("deep", "rem", "light", "awake"):
            entry["stages"][stage]["min"] += _stage_minutes(session, stage)

        heart_rate = (session.get("heart_rate") or {}).get("avg")
        if heart_rate and duration:
            entry["_hr_weighted"] += float(heart_rate) * duration
            entry["_hr_minutes"] += duration

    merged = []
    for entry in grouped.values():
        duration = entry["duration_min"]
        for stage in ("deep", "rem", "light", "awake"):
            minutes = entry["stages"][stage]["min"]
            entry["stages"][stage]["min"] = round(minutes, 1)
            entry["stages"][stage]["pct"] = round(minutes / duration * 100, 1) if duration else 0
        if entry["_hr_minutes"]:
            entry["heart_rate"] = {"avg": round(entry["_hr_weighted"] / entry["_hr_minutes"])}
        entry["duration_min"] = round(duration, 1)
        entry["sleep_min"] = round(entry["sleep_min"], 1)
        entry["quality"] = (
            "inferred"
            if entry["_segments"] and entry["_inferred_segments"] == entry["_segments"]
            else "mixed"
            if entry["_inferred_segments"]
            else "measured"
        )
        for key in ("_hr_weighted", "_hr_minutes", "_inferred_segments", "_segments"):
            entry.pop(key, None)
        merged.append(entry)

    return sorted(merged, key=lambda item: item["date"], reverse=True)


def _parse_clock(value: str | None) -> int | None:
    try:
        hours, minutes = str(value).split(":", 1)
        parsed = int(hours) * 60 + int(minutes)
    except (TypeError, ValueError):
        return None
    return parsed % 1440


def _circular_center(values: list[int]) -> int | None:
    if not values:
        return None
    angles = [value / 1440 * 2 * math.pi for value in values]
    x = sum(math.cos(angle) for angle in angles)
    y = sum(math.sin(angle) for angle in angles)
    if not x and not y:
        return round(median(values))
    angle = math.atan2(y, x)
    if angle < 0:
        angle += 2 * math.pi
    return round(angle / (2 * math.pi) * 1440) % 1440


def _circular_distance(left: int, right: int) -> int:
    distance = abs(left - right) % 1440
    return min(distance, 1440 - distance)


def _clock_label(value: int | None) -> str | None:
    if value is None:
        return None
    return f"{value // 60:02d}:{value % 60:02d}"


def compute_sleep_metrics(
    sessions: list[dict],
    *,
    target_min: int = 450,
    recent_nights: int = 7,
) -> dict:
    nights = aggregate_sleep_by_date(sessions)
    recent = nights[:recent_nights]
    count = len(recent)
    sleep_values = [round(float(night.get("sleep_min") or 0)) for night in recent]
    sleep_values = [value for value in sleep_values if value > 0]

    avg_sleep = round(sum(sleep_values) / len(sleep_values)) if sleep_values else 0
    debt = max(0, round(target_min * len(sleep_values) - sum(sleep_values)))
    repayment = min(60, round(debt / len(sleep_values))) if sleep_values and debt else 0

    bedtimes = [
        parsed
        for parsed in (_parse_clock(night.get("bedtime")) for night in recent)
        if parsed is not None
    ]
    waketimes = [
        parsed
        for parsed in (_parse_clock(night.get("waketime")) for night in recent)
        if parsed is not None
    ]
    bedtime_center = _circular_center(bedtimes)
    waketime_center = _circular_center(waketimes)
    bedtime_spread = (
        round(median(_circular_distance(value, bedtime_center) for value in bedtimes))
        if len(bedtimes) >= 3 and bedtime_center is not None
        else None
    )
    waketime_spread = (
        round(median(_circular_distance(value, waketime_center) for value in waketimes))
        if len(waketimes) >= 3 and waketime_center is not None
        else None
    )

    regularity_score = None
    if bedtime_spread is not None and waketime_spread is not None:
        average_spread = (bedtime_spread + waketime_spread) / 2
        regularity_score = round(max(0, 100 - average_spread / 1.5))

    if regularity_score is None:
        regularity_label = "мало данных"
    elif regularity_score >= 85:
        regularity_label = "стабильно"
    elif regularity_score >= 65:
        regularity_label = "умеренный разброс"
    else:
        regularity_label = "нестабильно"

    confidence = "high" if count >= 7 else "medium" if count >= 4 else "low"
    confidence_label = {
        "high": "высокая уверенность",
        "medium": "средняя уверенность",
        "low": "низкая уверенность",
    }[confidence]

    cumulative_debt = 0
    daily = []
    for night in reversed(recent):
        sleep_min = round(float(night.get("sleep_min") or 0))
        if sleep_min <= 0:
            continue
        cumulative_debt = max(0, cumulative_debt + target_min - sleep_min)
        daily.append(
            {
                "date": night["date"],
                "sleep_min": sleep_min,
                "delta_min": sleep_min - target_min,
                "debt_min": round(cumulative_debt),
            }
        )

    return {
        "target_min": target_min,
        "recent_nights_count": count,
        "avg_sleep_min": avg_sleep,
        "debt_min": debt,
        "recommended_sleep_min": target_min + repayment,
        "latest_sleep_min": sleep_values[0] if sleep_values else 0,
        "latest_delta_min": sleep_values[0] - target_min if sleep_values else None,
        "regularity_score": regularity_score,
        "regularity_label": regularity_label,
        "bedtime_spread_min": bedtime_spread,
        "waketime_spread_min": waketime_spread,
        "typical_bedtime": _clock_label(bedtime_center),
        "typical_waketime": _clock_label(waketime_center),
        "confidence": confidence,
        "confidence_label": confidence_label,
        "measured_nights": sum(night.get("quality") == "measured" for night in recent),
        "inferred_nights": sum(night.get("quality") != "measured" for night in recent),
        "daily": daily,
    }
