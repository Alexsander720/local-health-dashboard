"""
Генерация HTML-дашборда здоровья.

Использование:
    python build_dashboard.py              # собрать dashboard.html
    python build_dashboard.py --open       # + открыть в браузере
    python build_dashboard.py --gemini     # + вставить AI-анализ (кэш, недельный)
    python build_dashboard.py --force-gemini  # перегенерить AI
    python build_dashboard.py --all-periods   # генерить day+week+month заранее

Для интерактива (кнопки обновления, форма заметок) используй:
    python dashboard_server.py             # локальный API на 127.0.0.1:8787
"""

from __future__ import annotations

import argparse
import json
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

import storage_utils
from health_dashboard.domain.sleep import aggregate_sleep_by_date, compute_sleep_metrics
from health_dashboard.domain.body import normalize_measurements
from health_dashboard.demo_data import (
    build_demo_ai_caches,
    build_demo_data,
    build_demo_food_profile,
    build_demo_measurements,
)

BASE = Path(__file__).parent
DATA_DIR = BASE / "sleep-data"
JSON_PATH = DATA_DIR / "latest_sync.json"
HTML_PATH = BASE / "dashboard.html"
FOOD_PROFILE_PATH = BASE / "food_profile.json"
MEASUREMENTS_PATH = BASE / "body_measurements.json"

MEASUREMENT_FIELDS_RU = [
    ("chest_cm", "Грудь"),
    ("shoulders_cm", "Плечи"),
    ("waist_cm", "Талия"),
    ("hips_cm", "Бёдра"),
    ("biceps_cm", "Бицепс"),
    ("thigh_cm", "Бедро (объём)"),
    ("calf_cm", "Икра"),
    ("neck_cm", "Шея"),
    ("arm_length_cm", "Рука (длина)"),
    ("foot_cm", "Стопа"),
]


def load_measurements() -> dict:
    if not MEASUREMENTS_PATH.exists():
        return {}
    try:
        d = json.loads(MEASUREMENTS_PATH.read_text(encoding="utf-8"))
        fallback_date = datetime.fromtimestamp(
            MEASUREMENTS_PATH.stat().st_mtime
        ).strftime("%Y-%m-%d")
        return normalize_measurements(d, fallback_date=fallback_date)
    except Exception:
        return {}


DEFAULT_FOOD_PROFILE = {
    "profile_version": 2,
    "updated_at": None,
    "recipe_mode": True,
    "max_cook_minutes": 35,
    "cooking_energy": "low_to_medium",
    "goal": None,
    "priorities": [],
    "hard_exclusions": ["бобовые"],
    "soft_dislikes": [],
    "rare_ok": [],
    "untested": [],
    "liked_ingredients": [],
    "avoid_groups": ["legumes"],
    "disliked_ingredients": ["фасоль", "чечевица", "нут", "горох"],
    "preferred_proteins": ["курица", "яйца", "творог", "сыр", "рыба"],
    "preferred_sides": ["рис", "гречка", "макароны", "картофель", "овсянка"],
    "preferred_vegetables": ["огурец", "помидор", "замороженные овощи"],
    "preferred_fruits": [],
    "preferred_dishes": [],
    "comfort_formats": [],
    "satiety_foods": [],
    "snack_triggers": [],
    "real_life_treats": [],
    "safe_foods": [],
    "kitchen_equipment": [],
    "survey_answers": {},
    "notes": "Мягкий профиль: не требует вести склад продуктов дома. Используется только чтобы не советовать то, что человек не ест.",
}


POSITIVE_RATINGS = {"Люблю", "Нравится"}
ACCEPTABLE_RATINGS = POSITIVE_RATINGS | {"Нормально"}
HARD_AVOID_RATINGS = {"Не ем"}
SOFT_AVOID_RATINGS = {"Не люблю"}


def _unique(values) -> list:
    seen = set()
    out = []
    for value in values or []:
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    text = str(value).strip()
    return [text] if text else []


def _extract_survey_answers(profile: dict) -> dict:
    survey = profile.get("survey_answers")
    if isinstance(survey, dict) and survey:
        return dict(survey)
    return {
        key: value
        for key, value in (profile or {}).items()
        if isinstance(key, str) and key.startswith("q")
    }


def _rating_items(survey: dict, prefixes: tuple[str, ...], ratings: set[str]) -> list:
    out = []
    for key, value in survey.items():
        if not any(str(key).startswith(prefix) for prefix in prefixes):
            continue
        if str(value).strip() in ratings:
            out.append(str(key).split("_", 1)[1])
    return _unique(out)


def _all_rated_items(survey: dict, ratings: set[str]) -> list:
    out = []
    for key, value in survey.items():
        if "_" not in str(key):
            continue
        if str(value).strip() in ratings:
            out.append(str(key).split("_", 1)[1])
    return _unique(out)


def _avoid_group_for_item(item: str) -> str | None:
    text = item.lower()
    if "боб" in text or text in {"фасоль", "чечевица", "нут", "горох", "хумус", "соя", "тофу"}:
        return "legumes"
    if "орех" in text or "семеч" in text or text in {"арахис", "миндаль", "кешью", "фисташки", "фундук"}:
        return "nuts"
    if "печень" in text:
        return "liver"
    if "суп" in text or text == "борщ":
        return "soups"
    return None


def _extract_equipment(text: str) -> list:
    low = (text or "").lower()
    items = []
    for needle, label in (
        ("аэрогриль", "аэрогриль"),
        ("духов", "духовка"),
        ("плит", "плита"),
        ("бутербродниц", "бутербродница"),
        ("сковород", "сковорода"),
        ("микроволн", "микроволновка"),
    ):
        if needle in low:
            items.append(label)
    return _unique(items)


def _derive_food_profile_from_survey(survey: dict) -> dict:
    hard_from_questions = _as_list(survey.get("q9"))
    hard_from_ratings = _all_rated_items(survey, HARD_AVOID_RATINGS)
    soft_dislikes = _all_rated_items(survey, SOFT_AVOID_RATINGS)
    rare_ok = _all_rated_items(survey, {"Редко"})
    untested = _all_rated_items(survey, {"Не пробовал"})
    liked = _all_rated_items(survey, POSITIVE_RATINGS)

    hard_exclusions = _unique(hard_from_questions + hard_from_ratings)
    avoid_groups = _unique(
        group
        for group in (_avoid_group_for_item(item) for item in hard_exclusions + soft_dislikes)
        if group
    )

    protein_prefixes = ("q20_", "q22_", "q24_", "q26_")
    side_prefixes = ("q28_", "q30_")
    vegetable_prefixes = ("q32_",)
    fruit_prefixes = ("q34_",)
    dish_prefixes = ("q47_",)

    side_like = _rating_items(survey, side_prefixes, ACCEPTABLE_RATINGS)
    vegetable_like = _rating_items(survey, vegetable_prefixes, ACCEPTABLE_RATINGS)
    potato_sides = [x for x in vegetable_like if any(p in x.lower() for p in ("карто", "пюре"))]
    vegetable_like = [x for x in vegetable_like if x not in potato_sides]

    priorities = _as_list(survey.get("q2"))
    comfort_formats = _unique(_as_list(survey.get("q17")) + _as_list(survey.get("q19")) + _as_list(survey.get("q48")))
    safe_foods = _unique(_as_list(survey.get("q53")))

    notes_parts = []
    if survey.get("q1"):
        notes_parts.append(f"Цель питания: {survey.get('q1')}.")
    if priorities:
        notes_parts.append("Важнее всего: " + ", ".join(priorities) + ".")
    if hard_exclusions:
        notes_parts.append("Не предлагать: " + ", ".join(hard_exclusions) + ".")
    if comfort_formats:
        notes_parts.append("Комфортный формат: " + ", ".join(comfort_formats[:8]) + ".")
    if survey.get("q16"):
        notes_parts.append(f"Готовка: {survey.get('q16')}.")
    if survey.get("q52"):
        notes_parts.append(str(survey.get("q52")))
    if survey.get("q55"):
        notes_parts.append(str(survey.get("q55")))

    return {
        "profile_version": 2,
        "source": "deep_food_test",
        "goal": survey.get("q1"),
        "priorities": priorities,
        "recipe_mode": True,
        "max_cook_minutes": 35,
        "cooking_energy": "low_to_medium",
        "avoid_groups": avoid_groups or ["legumes"],
        "hard_exclusions": hard_exclusions,
        "soft_dislikes": soft_dislikes,
        "rare_ok": rare_ok,
        "untested": untested,
        "liked_ingredients": liked,
        "disliked_ingredients": _unique(hard_exclusions + soft_dislikes),
        "preferred_proteins": _rating_items(survey, protein_prefixes, ACCEPTABLE_RATINGS),
        "preferred_sides": _unique(side_like + potato_sides),
        "preferred_vegetables": vegetable_like,
        "preferred_fruits": _rating_items(survey, fruit_prefixes, ACCEPTABLE_RATINGS),
        "preferred_dishes": _unique(_rating_items(survey, dish_prefixes, ACCEPTABLE_RATINGS) + _as_list(survey.get("q57"))),
        "comfort_formats": comfort_formats,
        "satiety_foods": _as_list(survey.get("q49")),
        "snack_triggers": _as_list(survey.get("q51")),
        "real_life_treats": _as_list(survey.get("q42")),
        "safe_foods": safe_foods,
        "kitchen_equipment": _extract_equipment(str(survey.get("q59") or "")),
        "survey_answers": survey,
        "notes": " ".join(notes_parts),
    }


def normalize_food_profile(profile: dict | None) -> dict:
    source = dict(profile or {})
    survey = _extract_survey_answers(source)
    derived = _derive_food_profile_from_survey(survey) if survey else {}

    clean = dict(DEFAULT_FOOD_PROFILE)
    clean.update(derived)
    clean.update(source)
    if survey:
        clean["survey_answers"] = survey

    for key in (
        "priorities",
        "avoid_groups",
        "hard_exclusions",
        "soft_dislikes",
        "rare_ok",
        "untested",
        "liked_ingredients",
        "disliked_ingredients",
        "preferred_proteins",
        "preferred_sides",
        "preferred_vegetables",
        "preferred_fruits",
        "preferred_dishes",
        "comfort_formats",
        "satiety_foods",
        "snack_triggers",
        "real_life_treats",
        "safe_foods",
        "kitchen_equipment",
    ):
        clean[key] = _unique(clean.get(key))
    return clean


def load_data():
    with open(JSON_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_food_profile() -> dict:
    if not FOOD_PROFILE_PATH.exists():
        return dict(DEFAULT_FOOD_PROFILE)
    try:
        loaded = json.loads(FOOD_PROFILE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return dict(DEFAULT_FOOD_PROFILE)
    return normalize_food_profile(loaded)


def save_food_profile(profile: dict) -> dict:
    existing = {}
    if FOOD_PROFILE_PATH.exists():
        try:
            existing = json.loads(FOOD_PROFILE_PATH.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    base = dict(existing or {})
    base.update(profile or {})
    clean = normalize_food_profile(base)
    clean["updated_at"] = datetime.now().isoformat(timespec="seconds")
    storage_utils.atomic_write_json(FOOD_PROFILE_PATH, clean)
    return clean


def compute_insights(data):
    sleeps = aggregate_sleep_by_date(data.get("sleep_sessions", []))
    daily = data.get("daily_metrics", [])
    weights = data.get("weight_history", [])
    nutr = data.get("nutrition", [])
    goals = data.get("nutrition_goals", {})
    ins = {}
    if sleeps:
        ins["avg_sleep_min"] = round(sum(s["sleep_min"] for s in sleeps) / len(sleeps))
        ins["avg_deep_pct"] = round(sum(s["stages"]["deep"]["pct"] for s in sleeps) / len(sleeps), 1)
        ins["avg_rem_pct"] = round(sum(s["stages"]["rem"]["pct"] for s in sleeps) / len(sleeps), 1)
    if daily:
        steps = [d.get("activity", {}).get("steps", 0) for d in daily if d.get("activity")]
        if steps:
            ins["avg_steps"] = round(sum(steps) / len(steps))
    if weights:
        ins["current_weight"] = weights[0]["weight_kg"]
        now = datetime.fromisoformat(weights[0]["date"])
        month_ago = now - timedelta(days=30)
        for w in weights:
            if datetime.fromisoformat(w["date"]) <= month_ago:
                ins["weight_change_30d"] = round(weights[0]["weight_kg"] - w["weight_kg"], 1)
                break
        latest = weights[0]
        if "body_fat_pct" in latest:
            ins["body_fat"] = latest["body_fat_pct"]
            ins["muscle"] = latest.get("muscle_pct")
    if nutr:
        recent_kcal = [n["total_kcal"] for n in nutr[:14] if n["total_kcal"] > 100]
        if recent_kcal:
            ins["avg_kcal"] = round(sum(recent_kcal) / len(recent_kcal))
            if goals.get("kcal"):
                ins["kcal_vs_goal"] = round(ins["avg_kcal"] - goals["kcal"])
    return ins


def format_minutes_short(value: int | float | None) -> str:
    if value is None:
        return "—"
    total = max(0, round(float(value)))
    hours, minutes = divmod(total, 60)
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    if hours:
        return f"{hours} ч"
    return f"{minutes} мин"


def compute_energy_balance(data, days: int = 30, exercise_credit: float = 0.7, exercise_cap: int = 900) -> list:
    goals = data.get("nutrition_goals", {}) or {}
    base_goal = round(goals.get("kcal") or 0)
    if not base_goal:
        return []

    daily_by_date = {
        d.get("date"): d
        for d in data.get("daily_metrics", []) or []
        if d.get("date")
    }
    workout_by_date = {}
    for workout in data.get("workouts", []) or []:
        date = workout.get("date")
        if not date:
            continue
        workout_by_date[date] = workout_by_date.get(date, 0) + (workout.get("kcal") or 0)

    rows = []
    for item in data.get("nutrition", []) or []:
        date = item.get("date")
        if not date:
            continue
        eaten = round(item.get("total_kcal") or 0)
        activity_kcal = ((daily_by_date.get(date) or {}).get("activity") or {}).get("calories") or 0
        workout_kcal = workout_by_date.get(date, 0)
        exercise_raw = round(max(activity_kcal, workout_kcal))
        exercise_credit_kcal = round(min(exercise_raw * exercise_credit, exercise_cap))
        adjusted_goal = base_goal + exercise_credit_kcal
        base_delta = eaten - base_goal
        adjusted_delta = eaten - adjusted_goal
        rows.append({
            "date": date,
            "eaten": eaten,
            "base_goal": base_goal,
            "exercise_raw": exercise_raw,
            "exercise_credit": exercise_credit_kcal,
            "adjusted_goal": adjusted_goal,
            "base_delta": base_delta,
            "adjusted_delta": adjusted_delta,
            "over_base": max(base_delta, 0),
            "over_adjusted": max(adjusted_delta, 0),
            "remaining": adjusted_goal - eaten,
        })

    return sorted(rows, key=lambda x: x["date"])[-days:]


def compute_energy_rollups(balance_rows: list) -> dict:
    rows = sorted(balance_rows or [], key=lambda x: x.get("date") or "")

    def summarize(period_rows: list) -> dict:
        days = len(period_rows)
        remaining = round(sum(r.get("remaining") or 0 for r in period_rows))
        return {
            "days": days,
            "remaining": remaining,
            "avg_remaining": round(remaining / days) if days else 0,
            "over_adjusted": round(sum(r.get("over_adjusted") or 0 for r in period_rows)),
            "over_days": sum(1 for r in period_rows if (r.get("remaining") or 0) < 0),
            "under_days": sum(1 for r in period_rows if (r.get("remaining") or 0) >= 0),
            "base_delta": round(sum(r.get("base_delta") or 0 for r in period_rows)),
            "exercise_credit": round(sum(r.get("exercise_credit") or 0 for r in period_rows)),
        }

    return {
        "week": summarize(rows[-7:]),
        "month": summarize(rows[-30:]),
    }


def compute_nutrition_diary(data, days: int = 14) -> dict:
    goals = data.get("nutrition_goals", {}) or {}
    nutrition = sorted(data.get("nutrition", []) or [], key=lambda x: x.get("date") or "", reverse=True)
    balance_by_date = {
        row["date"]: row
        for row in compute_energy_balance(data, days=max(days, 30))
    }
    base_goal = round(goals.get("kcal") or 0)
    meal_weights = {"breakfast": 0.30, "lunch": 0.40, "dinner": 0.25, "snack": 0.05}
    meal_targets = {meal: round(base_goal * weight) for meal, weight in meal_weights.items()} if base_goal else {}

    diary_days = []
    for item in nutrition[:days]:
        date = item.get("date")
        balance = balance_by_date.get(date, {})
        meals = item.get("meals") or {}
        meal_rows = []
        for meal, info in meals.items():
            target = meal_targets.get(meal)
            kcal = round(info.get("kcal") or 0)
            meal_rows.append({
                "key": meal,
                "kcal": kcal,
                "target": target,
                "delta": kcal - target if target is not None else None,
                "protein_g": round(info.get("protein_g") or 0, 1),
                "fat_g": round(info.get("fat_g") or 0, 1),
                "carb_g": round(info.get("carb_g") or 0, 1),
                "items": info.get("items") or [],
            })
        diary_days.append({
            "date": date,
            "total_kcal": round(item.get("total_kcal") or 0),
            "protein_g": round(item.get("protein_g") or 0, 1),
            "fat_g": round(item.get("fat_g") or 0, 1),
            "carb_g": round(item.get("carb_g") or 0, 1),
            "macro_goals": {
                "protein_g": round(goals.get("protein_g") or 0, 1),
                "fat_g": round(goals.get("fat_g") or 0, 1),
                "carb_g": round(goals.get("carb_g") or 0, 1),
            },
            "meal_targets": meal_targets,
            "meals": meal_rows,
            **balance,
        })

    balance_rows = compute_energy_balance(data, days=30)
    return {
        "today": diary_days[0] if diary_days else {},
        "days": diary_days,
        "rollups": compute_energy_rollups(balance_rows),
    }


def format_chart_data(data):
    sleeps = list(reversed(aggregate_sleep_by_date(data.get("sleep_sessions", []))))
    daily = data.get("daily_metrics", [])
    weights = list(reversed(data.get("weight_history", [])))
    nutr = list(reversed(data.get("nutrition", [])))
    goals = data.get("nutrition_goals", {})
    workouts = data.get("workouts", []) or []

    sleep_chart = {
        "labels": [s["date"] for s in sleeps],
        "duration": [round(s["sleep_min"] / 60, 1) for s in sleeps],
        "deep": [s["stages"]["deep"]["min"] for s in sleeps],
        "rem": [s["stages"]["rem"]["min"] for s in sleeps],
        "light": [s["stages"]["light"]["min"] for s in sleeps],
        "awake": [s["stages"]["awake"]["min"] for s in sleeps],
        "hr": [s["heart_rate"]["avg"] if s.get("heart_rate") else None for s in sleeps],
    }

    now = datetime.now()
    cutoff = now - timedelta(days=90)
    w90 = [w for w in weights if datetime.fromisoformat(w["date"]) >= cutoff]
    weight_chart = {
        "labels": [w["date"] for w in w90],
        "weight": [w["weight_kg"] for w in w90],
        "fat": [w.get("body_fat_pct") for w in w90],
        "muscle": [w.get("muscle_pct") for w in w90],
    }

    nutr_recent = nutr[-30:]
    energy_balance = compute_energy_balance(data)
    nutr_chart = {
        "labels": [n["date"] for n in nutr_recent],
        "kcal": [n["total_kcal"] for n in nutr_recent],
        "protein": [n["protein_g"] for n in nutr_recent],
        "fat": [n["fat_g"] for n in nutr_recent],
        "carb": [n["carb_g"] for n in nutr_recent],
        "goal_kcal": goals.get("kcal", 0),
        "goal_protein": goals.get("protein_g", 0),
        "balance_labels": [d["date"] for d in energy_balance],
        "balance_eaten": [d["eaten"] for d in energy_balance],
        "balance_base_goal": [d["base_goal"] for d in energy_balance],
        "balance_adjusted_goal": [d["adjusted_goal"] for d in energy_balance],
        "balance_exercise_credit": [d["exercise_credit"] for d in energy_balance],
        "balance_remaining": [d["remaining"] for d in energy_balance],
        "balance_over_adjusted": [d["over_adjusted"] for d in energy_balance],
    }

    workout_by_date = {}
    for workout in workouts:
        date = workout.get("date")
        if not date:
            continue
        entry = workout_by_date.setdefault(date, {"min": 0, "kcal": 0})
        entry["min"] += workout.get("duration_min") or 0
        entry["kcal"] += workout.get("kcal") or 0

    activity_chart = {
        "labels": [d["date"] for d in daily],
        "steps": [d.get("activity", {}).get("steps", 0) for d in daily],
        "active_min": [d.get("activity", {}).get("active_min", 0) for d in daily],
        "calories": [d.get("activity", {}).get("calories", 0) for d in daily],
        "workout_min": [round(workout_by_date.get(d["date"], {}).get("min", 0), 1) for d in daily],
        "workout_kcal": [round(workout_by_date.get(d["date"], {}).get("kcal", 0), 1) for d in daily],
        "goal_steps": goals.get("steps_goal", 5000),
    }

    daily_health = {
        "labels": [d["date"] for d in daily],
        "hr_avg": [d["hr"]["avg"] if d.get("hr") else None for d in daily],
        "hr_min": [d["hr"]["min"] if d.get("hr") else None for d in daily],
        "hr_max": [d["hr"]["max"] if d.get("hr") else None for d in daily],
        "stress_avg": [d["stress"]["avg"] if d.get("stress") else None for d in daily],
        "stress_max": [d["stress"]["max"] if d.get("stress") else None for d in daily],
        "spo2_avg": [d["spo2"]["avg"] if d.get("spo2") else None for d in daily],
    }

    return {
        "sleep": sleep_chart, "weight": weight_chart,
        "nutrition": nutr_chart, "activity": activity_chart,
        "health": daily_health,
    }


AI_PERIODS = ("day", "week", "month", "sleep", "body", "nutrition", "foodprofile", "activity", "health")


def load_ai_caches() -> dict:
    """Загружает все 8 кэшей: 3 общих + 5 категорий."""
    out = {}
    for period in AI_PERIODS:
        txt = DATA_DIR / f"gemini_{period}.txt"
        meta_path = DATA_DIR / f"gemini_{period}.meta.json"
        if txt.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
                out[period] = {
                    "text": txt.read_text(encoding="utf-8"),
                    "iso": meta.get("iso"),
                    "model": meta.get("model"),
                }
            except Exception:
                pass
    return out


# compute_stale_meals удалён — был тупым (выдавал "орешки со сгущенкой 10д назад"
# вместо полезных советов). Заменён на compute_food_ideas() ниже,
# который смотрит на продуктовые ГРУППЫ (яйца/рис/овощи/рыба) с учётом дефицита БЖУ.


FOOD_GROUPS = (
    {
        "key": "eggs",
        "title": "Яйца / омлет",
        "patterns": ("яйц", "egg", "омлет"),
        "priority": "Белок",
        "idea": "Омлет 2-3 яйца с овощами или яйца + рис/гречка.",
        "dish": "Омлет с овощами и гарниром",
        "recipe": "2-3 яйца, горсть овощей, 150 г риса/гречки. Обжарить/потушить овощи, залить яйцами, гарнир отдельно.",
    },
    {
        "key": "lean_protein",
        "title": "Курица / рыба / нежирное мясо",
        "patterns": ("кур", "индей", "рыб", "лосос", "тунец", "хек", "треск", "говяд"),
        "priority": "Белок",
        "idea": "Курица или рыба + крупа + овощи, без добора калорий снеками.",
        "dish": "Куриная тарелка с рисом и овощами",
        "recipe": "Курица 180-220 г, рис/гречка 150-200 г, овощи. Быстро обжарить или запечь курицу, собрать в одну тарелку.",
    },
    {
        "key": "dairy",
        "title": "Творог / сырники / сладкий йогурт",
        "patterns": ("творог", "йогурт", "skyr", "сырники", "кефир"),
        "priority": "Белок",
        "idea": "Творог с бананом или сырники как нормальный белковый завтрак.",
        "dish": "Творог с бананом или быстрые сырники",
        "recipe": "Творог 200 г + банан/ягоды. Если сырники: творог, яйцо, немного муки, 5-7 минут на сковороде.",
    },
    {
        "key": "grains",
        "title": "Рис / гречка / овсянка",
        "patterns": ("рис", "греч", "овся", "каша", "булгур", "перлов", "круп"),
        "priority": "Гарнир",
        "idea": "Рис или гречка порцией 150-200 г к белку, чтобы еда была сытнее.",
        "dish": "Гречка или рис с белком",
        "recipe": "Сварить крупу на 2-3 порции заранее, добавлять к курице/яйцам/рыбе и овощам.",
    },
    {
        "key": "vegetables",
        "title": "Овощи",
        "patterns": ("огур", "помид", "овощ", "салат", "капуст", "морков", "спарж", "брок", "перец"),
        "priority": "Клетчатка",
        "idea": "Большая миска салата или замороженные овощи к основному блюду.",
        "dish": "Овощная добавка к любому основному блюду",
        "recipe": "Огурец/помидор или замороженные овощи 200-300 г. Добавить к курице, яйцам или гарниру.",
    },
    {
        "key": "legumes",
        "title": "Фасоль / чечевица / нут",
        "patterns": ("фасол", "чечев", "нут", "горох", "боб"),
        "priority": "Белок + клетчатка",
        "idea": "Чечевичный суп, фасоль с рисом или нут как гарнир.",
        "dish": "Чечевичный суп или фасоль с рисом",
        "recipe": "Чечевица/фасоль + рис + овощи. Подходит только если эти продукты норм заходят.",
    },
    {
        "key": "fruit",
        "title": "Фрукты / ягоды",
        "patterns": ("банан", "яблок", "ягод", "черник", "лохин", "апельс", "груш"),
        "priority": "Микронутриенты",
        "idea": "Фрукт или ягоды к творогу/йогурту вместо сладостей.",
        "dish": "Творог или йогурт с фруктом",
        "recipe": "Творог/йогурт 200 г + банан/яблоко/ягоды. Быстро, без готовки, лучше чем добирать сладким.",
    },
)


def compute_food_ideas(data, days_back: int = 30, profile: dict | None = None) -> list:
    """Suggest useful meals by food groups, not random stale snack item names."""
    profile = profile or load_food_profile()
    avoid_groups = set(profile.get("avoid_groups") or [])
    disliked = [str(x).lower() for x in (profile.get("disliked_ingredients") or [])]
    recipe_mode = bool(profile.get("recipe_mode", True))
    nutr = data.get("nutrition", [])
    goals = data.get("nutrition_goals", {})
    today = datetime.now().date()
    cutoff = today - timedelta(days=days_back)
    seen = {
        g["key"]: {"last_date": None, "count": 0}
        for g in FOOD_GROUPS
    }
    complete_days = []

    for n in nutr:
        try:
            d = datetime.fromisoformat(n["date"]).date()
        except Exception:
            continue
        if d < cutoff:
            continue
        if n.get("total_kcal", 0) > 300:
            complete_days.append(n)
        names = []
        for meal_info in (n.get("meals") or {}).values():
            for item in meal_info.get("items") or []:
                name = (item.get("name") or "").lower()
                if name:
                    names.append(name)
        for group in FOOD_GROUPS:
            if any(p in name for name in names for p in group["patterns"]):
                seen[group["key"]]["count"] += 1
                if seen[group["key"]]["last_date"] is None or d > seen[group["key"]]["last_date"]:
                    seen[group["key"]]["last_date"] = d

    avg_protein = None
    avg_fiber = None
    if complete_days:
        avg_protein = sum(d.get("protein_g", 0) for d in complete_days) / len(complete_days)
        fiber_values = [d.get("fiber_g") for d in complete_days if d.get("fiber_g") is not None]
        if fiber_values:
            avg_fiber = sum(fiber_values) / len(fiber_values)

    protein_goal = goals.get("protein_g") or 0
    low_protein = bool(protein_goal and avg_protein is not None and avg_protein < protein_goal * 0.75)
    low_fiber = bool(avg_fiber is not None and avg_fiber < 20)

    ideas = []
    for group in FOOD_GROUPS:
        if group["key"] in avoid_groups:
            continue
        searchable = " ".join([group["title"], group["idea"], group.get("dish", "")]).lower()
        if any(x and x in searchable for x in disliked):
            continue
        stat = seen[group["key"]]
        last_date = stat["last_date"]
        days_ago = (today - last_date).days if last_date else None
        reason_bits = []
        score = 0
        if days_ago is None:
            reason_bits.append(f"не видно за {days_back} дней")
            score += 30
        elif days_ago >= 10:
            reason_bits.append(f"последний раз {days_ago} дн. назад")
            score += 20
        elif days_ago >= 5:
            reason_bits.append(f"давно не было: {days_ago} дн. назад")
            score += 10
        else:
            reason_bits.append(f"было недавно: {days_ago} дн. назад")

        if group["priority"].startswith("Белок") and low_protein:
            reason_bits.append(f"средний белок {avg_protein:.0f} г при цели {protein_goal:.0f} г")
            score += 25
        if group["priority"] == "Клетчатка" and low_fiber:
            reason_bits.append(f"клетчатки мало: около {avg_fiber:.0f} г/день")
            score += 25
        if group["key"] in ("eggs", "lean_protein", "vegetables", "grains"):
            score += 5

        ideas.append({
            "title": group["title"],
            "priority": group["priority"],
            "reason": "; ".join(reason_bits),
            "idea": group["idea"],
            "dish": group.get("dish", group["title"]),
            "recipe": group.get("recipe", "") if recipe_mode else "",
            "days_ago": days_ago,
            "score": score,
        })

    ideas.sort(key=lambda x: (-x["score"], 999 if x["days_ago"] is None else -x["days_ago"]))
    return [{k: v for k, v in idea.items() if k != "score"} for idea in ideas[:6]]


def compute_notes_status(data, recent_days: int = 7) -> dict:
    feelings = data.get("feelings", []) or []
    today = datetime.now().date()
    cutoff = today - timedelta(days=recent_days)
    manual = 0
    yazio = 0
    recent = 0
    latest = None
    for f in feelings:
        if f.get("manual_note"):
            manual += 1
        if f.get("yazio_note"):
            yazio += 1
        try:
            d = datetime.fromisoformat((f.get("date") or "").strip('"')).date()
        except Exception:
            continue
        latest = d if latest is None or d > latest else latest
        if d >= cutoff and (f.get("manual_note") or f.get("yazio_note")):
            recent += 1

    total = manual + yazio
    if total == 0:
        message = "Заметок пока нет в итоговых данных. Для статического HTML добавляй через note.py или запускай dashboard_server.py."
    elif recent == 0:
        message = f"Есть {total} заметок, но нет свежих за последние {recent_days} дней. YAZIO сейчас отдаёт пустые notes/tags."
    elif yazio == 0:
        message = f"Свежие PC-заметки есть, но YAZIO-заметки в базе сейчас пустые."
    else:
        message = f"Заметки подключены: свежих {recent}, YAZIO {yazio}, с ПК {manual}."

    return {
        "total": total,
        "manual": manual,
        "yazio": yazio,
        "recent": recent,
        "has_recent": recent > 0,
        "latest": latest.isoformat() if latest else None,
        "message": message,
    }


def compute_sync_notice(data: dict, today=None) -> dict:
    today = today or datetime.now().date()
    if isinstance(today, datetime):
        today = today.date()
    status = data.get("sync_status") or {}
    nutrition = data.get("nutrition") or []
    last_nutrition_date = nutrition[0].get("date") if nutrition else None
    parts = []

    if status.get("used_cached_dbs"):
        parts.append("телефон недоступен, данные из кэша")

    if last_nutrition_date:
        try:
            last_date = datetime.fromisoformat(last_nutrition_date).date()
            if last_date < today - timedelta(days=1):
                parts.append(f"питание до {last_nutrition_date}")
        except Exception:
            pass

    text = " · ".join(parts)
    return {
        "class": "warn" if text else "",
        "text": text,
    }


def compute_section_kpis(data, insights, sleep_metrics, food_profile, notes_status) -> dict:
    def mean(values, digits=0):
        clean = [float(value) for value in values if value is not None]
        if not clean:
            return None
        result = sum(clean) / len(clean)
        return round(result, digits)

    def metric(label, value, unit, delta, icon, color, tone=""):
        return {
            "label": label,
            "value": value if value is not None else "—",
            "unit": unit,
            "delta": delta,
            "icon": icon,
            "color": color,
            "tone": tone,
        }

    daily = sorted(
        (row for row in data.get("daily_metrics", []) or [] if row.get("date")),
        key=lambda row: row["date"],
        reverse=True,
    )[:7]
    nutrition = [
        row for row in sorted(
            data.get("nutrition", []) or [],
            key=lambda row: row.get("date") or "",
            reverse=True,
        )[:7]
        if (row.get("total_kcal") or 0) > 100
    ]
    goals = data.get("nutrition_goals", {}) or {}
    workouts = data.get("workouts", []) or []

    activity_rows = [row.get("activity") or {} for row in daily if row.get("activity")]
    health_rows = daily
    avg_active = mean([row.get("active_min") for row in activity_rows])
    avg_distance = mean([(row.get("distance_m") or 0) / 1000 for row in activity_rows], 1)
    avg_hr = mean([(row.get("hr") or {}).get("avg") for row in health_rows])
    avg_stress = mean([(row.get("stress") or {}).get("avg") for row in health_rows])
    avg_spo2 = mean([(row.get("spo2") or {}).get("avg") for row in health_rows], 1)

    latest_date = max((row.get("date") or "" for row in daily), default="")
    recent_workouts = workouts
    if latest_date:
        cutoff = (datetime.fromisoformat(latest_date).date() - timedelta(days=6)).isoformat()
        recent_workouts = [
            workout for workout in workouts
            if cutoff <= (workout.get("date") or "") <= latest_date
        ]

    current_weight = insights.get("current_weight")
    weight_goal = goals.get("weight_goal_kg")
    goal_gap = (
        round(current_weight - weight_goal, 1)
        if current_weight is not None and weight_goal
        else None
    )
    profile_answers = len(food_profile.get("survey_answers") or {})
    profile_likes = len(set(
        (food_profile.get("liked_ingredients") or [])
        + (food_profile.get("preferred_proteins") or [])
        + (food_profile.get("preferred_sides") or [])
        + (food_profile.get("preferred_vegetables") or [])
    ))
    profile_exclusions = len(set(
        (food_profile.get("hard_exclusions") or [])
        + (food_profile.get("avoid_groups") or [])
        + (food_profile.get("disliked_ingredients") or [])
    ))

    return {
        "sleep": [
            metric("Средний сон", round((sleep_metrics.get("avg_sleep_min") or 0) / 60, 1) if sleep_metrics.get("avg_sleep_min") else None, "ч", f"{sleep_metrics.get('recent_nights_count', 0)} ночей", "sleep", "#8c7cff"),
            metric("Глубокий", insights.get("avg_deep_pct"), "%", "ориентир 13–23%", "pulse", "#7868ff"),
            metric("REM", insights.get("avg_rem_pct"), "%", "ориентир 20–25%", "brain", "#a578ff"),
            metric("ЧСС во сне", mean([(row.get("heart_rate") or {}).get("avg") for row in aggregate_sleep_by_date(data.get("sleep_sessions", []))]), "уд/мин", "среднее по ночам", "health", "#ff648a"),
        ],
        "body": [
            metric("Вес", current_weight, "кг", "текущая запись", "weight", "#2dd4d0"),
            metric("Жир", insights.get("body_fat"), "%", "состав тела", "droplet", "#42d7b6"),
            metric("Мышцы", insights.get("muscle"), "%", "состав тела", "body", "#45d99a"),
            metric("До цели", goal_gap, "кг", f"цель {weight_goal} кг" if weight_goal else "цель не задана", "target", "#f5c36b", "bad" if goal_gap and goal_gap > 0 else "ok"),
        ],
        "nutrition": [
            metric("Калории", mean([row.get("total_kcal") for row in nutrition]), "ккал", "среднее за 7 записей", "fire", "#ff934d"),
            metric("Белок", mean([row.get("protein_g") for row in nutrition]), "г", f"цель {goals.get('protein_g')} г" if goals.get("protein_g") else "цель не задана", "nutrition", "#4ade80"),
            metric("Жиры", mean([row.get("fat_g") for row in nutrition]), "г", f"цель {goals.get('fat_g')} г" if goals.get("fat_g") else "цель не задана", "droplet", "#f5c36b"),
            metric("Углеводы", mean([row.get("carb_g") for row in nutrition]), "г", f"цель {goals.get('carb_g')} г" if goals.get("carb_g") else "цель не задана", "chart-donut", "#60a5fa"),
        ],
        "foodprofile": [
            metric("Ответы", profile_answers, "", "в пищевом профиле", "profile", "#45d99a"),
            metric("Предпочтения", profile_likes, "", "учтено продуктов", "health", "#4ade80"),
            metric("Исключения", profile_exclusions, "", "не предлагать", "close", "#f87171"),
            metric("Рецепты", "вкл" if food_profile.get("recipe_mode", True) else "выкл", "", f"до {food_profile.get('max_cook_minutes') or 25} минут", "nutrition", "#f5c36b"),
        ],
        "activity": [
            metric("Шаги / день", mean([row.get("steps") for row in activity_rows]), "", "среднее за 7 дней", "steps", "#3f8cff"),
            metric("Активные минуты", avg_active, "мин", "в среднем за день", "activity", "#2dd4d0"),
            metric("Дистанция", avg_distance, "км", "в среднем за день", "target", "#60a5fa"),
            metric("Тренировки", len(recent_workouts), "", "за последние 7 дней", "pulse", "#a578ff"),
        ],
        "health": [
            metric("Средняя ЧСС", avg_hr, "уд/мин", "за последние 7 дней", "health", "#ff648a"),
            metric("Стресс", avg_stress, "", "средний уровень", "brain", "#f472b6"),
            metric("SpO₂", avg_spo2, "%", "среднее значение", "lungs", "#a578ff"),
            metric("Записей", len(health_rows), "дн", "доступно за неделю", "database", "#60a5fa"),
        ],
        "notes": [
            metric("Всего заметок", notes_status.get("total"), "", "единая хронология", "notes", "#e8ad42"),
            metric("Свежие", notes_status.get("recent"), "", "за последние 7 дней", "clock", "#f5c36b", "ok" if notes_status.get("recent") else "warn"),
            metric("YAZIO", notes_status.get("yazio"), "", "импортировано", "nutrition", "#ff934d"),
            metric("С ПК", notes_status.get("manual"), "", "добавлено вручную", "message", "#8c7cff"),
        ],
    }


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Health Dashboard</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='18' fill='%230b1526'/%3E%3Cpath d='M10 33h10l5-12 9 25 6-13h14' fill='none' stroke='%238c7cff' stroke-width='5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@500;600&family=Manrope:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root {{
    --bg0:#090d10; --bg1:#11171d; --bg2:#172027;
    --panel:rgba(246,248,241,0.055);
    --panel-hi:rgba(246,248,241,0.09);
    --border:rgba(237,242,232,0.10);
    --border-hi:rgba(125,211,199,0.38);
    --text:#eef4ee; --muted:#9da99f; --dim:#68736d;
    --accent:#7dd3c7; --accent2:#f5c36b; --accent-warm:#f29f67;
    --ok:#4ade80; --warn:#fbbf24; --bad:#f87171;
    --deep:#6366f1; --rem:#a78bfa; --light:#60a5fa; --awake:#f87171;
}}
* {{ box-sizing:border-box; margin:0; padding:0; }}
html,body {{ background:var(--bg0); color:var(--text); font-family:-apple-system,'Segoe UI',Roboto,system-ui,sans-serif; }}
body {{
    background:
        linear-gradient(180deg, #090d10 0%, #10161c 46%, #090d10 100%),
        var(--bg0);
    min-height:100vh;
    padding:20px 24px 60px;
}}
.container {{ max-width:1440px; margin:0 auto; }}
a {{ color:var(--accent); text-decoration:none; }}

/* Header */
header {{
    display:grid;
    grid-template-columns: 1fr auto;
    gap:20px;
    align-items:center;
    margin-bottom:20px;
    padding:18px 22px;
    background:var(--panel);
    border:1px solid var(--border);
    border-radius:8px;
    backdrop-filter:blur(20px);
}}
.brand h1 {{
    font-size:22px; font-weight:700; letter-spacing:-0.3px;
    background:linear-gradient(135deg, var(--accent), var(--accent2));
    -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent;
}}
.brand .sub {{ color:var(--muted); font-size:12px; margin-top:2px; }}
.head-controls {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; justify-content:flex-end; }}
.head-controls .sync-info {{ color:var(--dim); font-size:11px; text-align:right; }}
.head-controls .btn {{ font-size:12px; padding:7px 12px; }}
.sync-notice {{
    grid-column:1 / -1;
    color:var(--warn);
    font-size:12px;
    border-top:1px solid var(--border);
    padding-top:10px;
    display:none;
}}
.sync-notice.warn {{ display:block; }}
.demo-banner {{
    display:flex;
    align-items:center;
    gap:8px;
    margin:-8px 0 16px;
    padding:10px 14px;
    border:1px solid rgba(74,222,128,.28);
    border-radius:10px;
    background:rgba(74,222,128,.07);
    color:#a7f3d0;
    font-size:12px;
}}
.demo-banner[hidden] {{ display:none; }}

.btn {{
    background:var(--panel);
    border:1px solid var(--border);
    color:var(--text);
    padding:9px 14px; border-radius:7px;
    font-size:13px; font-family:inherit;
    cursor:pointer; transition:all 0.15s;
    display:inline-flex; align-items:center; gap:6px;
}}
.btn:hover {{ background:var(--panel-hi); border-color:var(--border-hi); transform:translateY(-1px); }}
.btn:active {{ transform:translateY(0); }}
.btn.primary {{ background:linear-gradient(135deg, rgba(139,158,255,0.2), rgba(201,158,255,0.2)); border-color:var(--border-hi); }}
.btn.sm {{ padding:5px 10px; font-size:11px; }}
.btn.icon {{ padding:7px 9px; }}
.btn[disabled] {{ opacity:0.5; cursor:wait; }}

/* AI compact card */
.ai-card {{
    background:linear-gradient(135deg, rgba(139,158,255,0.08), rgba(201,158,255,0.06));
    border:1px solid var(--border);
    border-left:3px solid var(--accent);
    border-radius:8px;
    padding:14px 18px;
    margin-bottom:20px;
    display:grid;
    grid-template-columns: auto 1fr auto;
    gap:16px;
    align-items:center;
}}
.ai-card .ai-icon {{ font-size:22px; }}
.ai-card .ai-body {{ min-width:0; }}
.ai-card .ai-summary-line {{
    font-size:14px; color:var(--text); line-height:1.4;
    overflow:hidden; text-overflow:ellipsis; display:-webkit-box;
    -webkit-line-clamp:2; -webkit-box-orient:vertical;
}}
.ai-card .ai-meta {{ color:var(--dim); font-size:11px; margin-top:4px; }}
.ai-card .ai-controls {{ display:flex; gap:6px; align-items:center; flex-shrink:0; }}
.period-switch {{
    display:flex; background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:2px;
}}
.period-switch button {{
    background:transparent; border:none; color:var(--muted); font-family:inherit;
    padding:5px 11px; border-radius:6px; font-size:12px; cursor:pointer; transition:all 0.15s;
}}
.period-switch button:hover {{ color:var(--text); }}
.period-switch button.active {{ background:rgba(139,158,255,0.25); color:var(--text); }}

/* Stats strip */
.stats {{
    display:grid; grid-template-columns:repeat(auto-fit, minmax(145px, 1fr));
    gap:12px; margin-bottom:22px;
}}
.stat {{
    background:var(--panel); border:1px solid var(--border);
    border-radius:8px; padding:14px 16px;
    transition:all 0.15s;
}}
.stat:hover {{ transform:translateY(-1px); border-color:var(--border-hi); }}
.stat .label {{ color:var(--muted); font-size:10.5px; text-transform:uppercase; letter-spacing:0.8px; margin-bottom:6px; }}
.stat .value {{ font-size:22px; font-weight:700; letter-spacing:-0.5px; }}
.stat .value .u {{ font-size:12px; color:var(--muted); font-weight:500; margin-left:2px; }}
.stat .delta {{ font-size:11px; color:var(--dim); margin-top:3px; }}
.stat .delta.ok {{ color:var(--ok); }}
.stat .delta.warn {{ color:var(--warn); }}
.stat .delta.bad {{ color:var(--bad); }}

/* Tabs */
.tabs {{
    display:flex; gap:4px; margin-bottom:16px; padding:4px;
    background:var(--panel); border:1px solid var(--border); border-radius:8px;
    overflow-x:auto;
}}
.tab {{
    background:transparent; border:none; color:var(--muted);
    padding:9px 16px; border-radius:6px; font-size:13px; font-family:inherit;
    cursor:pointer; transition:all 0.15s; white-space:nowrap;
    display:flex; align-items:center; gap:6px;
}}
.tab:hover {{ color:var(--text); }}
.tab.active {{ background:rgba(139,158,255,0.2); color:var(--text); }}
.tab-panel {{ display:none; }}
.tab-panel.active {{ display:block; }}

/* Sleep command strip */
.sleep-metrics {{
    display:grid;
    grid-template-columns:1.15fr .85fr 1fr 1.15fr;
    gap:12px;
    margin-bottom:12px;
}}
.sleep-metric {{
    position:relative;
    min-width:0;
    min-height:154px;
    overflow:hidden;
    padding:16px 17px;
    border:1px solid var(--border);
    border-radius:var(--radius-lg, 18px);
    background:
        radial-gradient(circle at 100% 0, color-mix(in srgb,var(--sleep-color,#8c7cff) 16%,transparent), transparent 42%),
        var(--panel);
}}
.sleep-metric:before {{
    content:"";
    position:absolute;
    inset:0 auto 0 0;
    width:2px;
    background:var(--sleep-color,#8c7cff);
}}
.sleep-metric-head {{
    display:flex;
    justify-content:space-between;
    align-items:center;
    gap:10px;
    color:var(--muted);
    font-size:9.5px;
    font-weight:750;
    text-transform:uppercase;
    letter-spacing:.08em;
}}
.sleep-metric-head .ui-icon {{
    width:16px;
    height:16px;
    color:var(--sleep-color,#8c7cff);
}}
.sleep-metric-title {{ display:flex; align-items:center; gap:7px; }}
.sleep-confidence {{
    padding:3px 6px;
    border:1px solid var(--border);
    border-radius:999px;
    color:var(--dim);
    font:600 8px/1 'IBM Plex Mono',monospace;
    letter-spacing:.04em;
    white-space:nowrap;
}}
.sleep-confidence.high {{ color:var(--ok); border-color:rgba(74,222,128,.24); }}
.sleep-confidence.medium {{ color:var(--warn); border-color:rgba(251,191,36,.24); }}
.sleep-metric-value {{
    margin-top:14px;
    color:var(--text);
    font-size:27px;
    font-weight:800;
    line-height:1;
    letter-spacing:-.04em;
}}
.sleep-metric-value .unit {{
    margin-left:3px;
    color:var(--muted);
    font-size:12px;
    font-weight:600;
    letter-spacing:0;
}}
.sleep-metric-note {{
    margin-top:9px;
    color:var(--muted);
    font-size:11px;
    line-height:1.45;
}}
.sleep-metric-note strong {{ color:var(--text); font-weight:700; }}
.sleep-debt-bars {{
    height:28px;
    margin-top:12px;
    display:flex;
    align-items:flex-end;
    gap:4px;
}}
.sleep-debt-bars span {{
    flex:1;
    min-height:4px;
    border-radius:3px 3px 1px 1px;
    background:linear-gradient(180deg,var(--sleep-color,#8c7cff),color-mix(in srgb,var(--sleep-color,#8c7cff) 42%,transparent));
    opacity:.88;
}}
.sleep-window {{
    display:flex;
    align-items:center;
    gap:8px;
    margin-top:16px;
    color:var(--text);
    font:700 22px/1 'Manrope',sans-serif;
}}
.sleep-window .line {{ flex:1; height:1px; background:linear-gradient(90deg,var(--sleep-color),transparent); }}

/* Charts grid */
.charts-grid {{
    display:grid; grid-template-columns:repeat(auto-fit, minmax(480px, 1fr));
    gap:14px;
}}
.chart-card {{
    background:var(--panel); border:1px solid var(--border);
    border-radius:8px; padding:18px 20px;
}}
.chart-card h2 {{
    font-size:14px; font-weight:600; margin-bottom:12px;
    color:var(--text); display:flex; align-items:center; gap:8px;
}}
.chart-card h2 .icon {{ font-size:16px; }}
.chart-card h2 .hint {{ color:var(--dim); font-size:11px; font-weight:400; margin-left:auto; }}
.chart-wrapper {{ position:relative; height:260px; }}
.wide {{ grid-column:1 / -1; }}

/* Energy balance */
.energy-panel {{
    display:grid; grid-template-columns:repeat(auto-fit, minmax(155px, 1fr));
    gap:10px; margin:14px 0;
}}
.energy-card {{
    background:linear-gradient(180deg, rgba(255,255,255,0.055), rgba(255,255,255,0.025));
    border:1px solid var(--border); border-radius:8px; padding:13px 14px;
}}
.energy-card .label {{ color:var(--muted); font-size:10.5px; text-transform:uppercase; letter-spacing:0.8px; margin-bottom:6px; }}
.energy-card .value {{ font-size:22px; font-weight:700; color:var(--text); }}
.energy-card .value .u {{ font-size:11px; color:var(--muted); font-weight:500; margin-left:2px; }}
.energy-card .note {{ color:var(--dim); font-size:11px; margin-top:4px; line-height:1.35; }}
.energy-card.ok {{ border-color:rgba(74,222,128,0.35); }}
.energy-card.warn {{ border-color:rgba(251,191,36,0.35); }}
.energy-card.bad {{ border-color:rgba(248,113,113,0.42); }}
.energy-card.ok .value {{ color:var(--ok); }}
.energy-card.warn .value {{ color:var(--warn); }}
.energy-card.bad .value {{ color:var(--bad); }}
.nutrition-diary {{
    display:grid; gap:14px;
}}
.diary-nav {{
    display:grid;
    grid-template-columns:auto minmax(220px, 1fr) auto;
    gap:10px;
    align-items:center;
    padding:10px 0 4px;
}}
.diary-nav button {{
    width:38px; height:38px;
    display:grid; place-items:center;
    border-radius:8px;
    background:rgba(255,255,255,0.04);
    border:1px solid var(--border);
    color:var(--text);
    cursor:pointer;
    font:700 18px/1 inherit;
    transition:all 0.15s;
}}
.diary-nav button .ui-icon {{ width:17px; height:17px; }}
.diary-nav button:hover:not(:disabled) {{ border-color:var(--border-hi); background:rgba(139,158,255,0.15); }}
.diary-nav button:disabled {{ opacity:0.35; cursor:default; }}
.diary-nav .date-box {{ text-align:center; min-width:0; }}
.diary-nav .date-main {{ font-size:15px; font-weight:750; color:var(--text); }}
.diary-nav .date-sub {{ color:var(--dim); font-size:11px; margin-top:3px; }}
.diary-summary {{
    display:grid;
    grid-template-columns:minmax(320px, 0.95fr) minmax(360px, 1.15fr);
    gap:14px;
    align-items:stretch;
    padding:2px 0 14px;
    border-bottom:1px solid var(--border);
}}
.diary-overview {{
    display:grid;
    grid-template-columns:repeat(3, minmax(0, 1fr));
    gap:8px;
}}
.diary-metric,
.diary-goal-state {{
    min-width:0;
    min-height:112px;
    padding:12px;
    border:1px solid var(--border);
    border-radius:10px;
    background:rgba(255,255,255,0.025);
    display:flex;
    flex-direction:column;
    justify-content:space-between;
}}
.diary-metric .metric-head,
.diary-goal-state .metric-head {{
    display:flex;
    align-items:center;
    gap:7px;
    color:var(--muted);
    font-size:10px;
    text-transform:uppercase;
    letter-spacing:0.65px;
}}
.diary-metric .metric-head .ui-icon,
.diary-goal-state .metric-head .ui-icon {{
    width:16px;
    height:16px;
    color:var(--section-accent);
}}
.diary-metric .num {{ font-size:25px; font-weight:780; color:var(--text); }}
.diary-metric .num .u {{ font-size:11px; color:var(--muted); font-weight:550; margin-left:2px; }}
.diary-metric .cap,
.diary-goal-state .cap {{ color:var(--dim); font-size:10.5px; line-height:1.35; }}
.diary-goal-state {{
    border-color:rgba(245,195,107,0.22);
    background:linear-gradient(145deg, rgba(245,195,107,0.075), rgba(255,255,255,0.02));
}}
.diary-goal-state .goal-title {{ font-size:14px; font-weight:750; color:var(--accent2); }}
.diary-gauge {{
    width:112px; height:112px; border-radius:50%; margin:auto;
    display:grid; place-items:center; position:relative;
    background:conic-gradient(var(--gauge-color) var(--gauge-pct), rgba(255,255,255,0.08) 0);
}}
.diary-gauge:after {{
    content:""; position:absolute; inset:12px; border-radius:50%; background:var(--bg1);
    border:1px solid var(--border);
}}
.diary-gauge .inside {{ position:relative; z-index:1; text-align:center; }}
.diary-gauge .inside .big {{ font-size:23px; font-weight:800; }}
.diary-gauge .inside .small {{ color:var(--muted); font-size:10px; margin-top:2px; }}
.macro-bars {{
    display:grid;
    grid-template-columns:repeat(3, minmax(0, 1fr));
    gap:8px;
    align-content:start;
}}
.macro-line {{
    display:grid;
    gap:7px;
    min-width:0;
    padding:10px;
    border:1px solid var(--border);
    border-radius:9px;
    background:rgba(255,255,255,0.02);
}}
.macro-line .top {{ display:flex; justify-content:space-between; font-size:12px; color:var(--muted); }}
.macro-line .top span:last-child {{ color:var(--text); font-weight:650; }}
.macro-line.no-goal {{ min-height:62px; align-content:center; }}
.macro-line.no-goal .top {{ display:grid; gap:4px; }}
.macro-line.no-goal .top span:last-child {{ font-size:17px; }}
.macro-track {{ height:8px; border-radius:999px; background:rgba(255,255,255,0.12); overflow:hidden; }}
.macro-fill {{ height:100%; width:min(var(--pct), 130%); border-radius:999px; background:var(--accent); }}
.macro-fill.warn {{ background:var(--warn); }}
.macro-fill.bad {{ background:var(--bad); }}
.macro-bars .energy-card {{
    grid-column:1 / -1;
    padding:10px 12px;
}}
.nutrition-balance.is-unavailable {{ display:none; }}
.nutrition-calories.is-wide {{ grid-column:1 / -1; }}
.diary-rollups {{
    display:grid; grid-template-columns:repeat(auto-fit, minmax(210px, 1fr)); gap:10px;
}}
.diary-rollup {{
    border:1px solid var(--border); border-radius:8px; padding:11px 12px;
    background:rgba(255,255,255,0.025);
}}
.diary-rollup .label {{ color:var(--muted); font-size:10.5px; text-transform:uppercase; letter-spacing:0.8px; }}
.diary-rollup .value {{ font-size:20px; font-weight:750; margin-top:5px; }}
.diary-rollup .value.ok {{ color:var(--ok); }}
.diary-rollup .value.bad {{ color:var(--bad); }}
.diary-rollup .note {{ color:var(--dim); font-size:11px; margin-top:4px; }}
.nutrition-days {{
    display:flex; gap:7px; overflow-x:auto; padding-bottom:2px;
}}
.nutrition-day {{
    flex:0 0 auto;
    min-width:118px;
    border:1px solid var(--border); border-radius:8px; padding:8px 10px;
    background:rgba(255,255,255,0.025);
    cursor:pointer;
    text-align:left;
    color:var(--muted);
    font-family:inherit;
    transition:all 0.15s;
}}
.nutrition-day:hover {{ border-color:var(--border-hi); transform:translateY(-1px); }}
.nutrition-day.active {{
    border-color:rgba(125,225,217,0.65);
    background:rgba(125,225,217,0.10);
    color:var(--text);
}}
.nutrition-day .date {{ color:var(--accent2); font-size:12px; font-weight:650; }}
.nutrition-day .line {{ color:var(--muted); font-size:11.5px; margin-top:4px; }}
.nutrition-day .delta.ok {{ color:var(--ok); }}
.nutrition-day .delta.bad {{ color:var(--bad); }}

/* Meals */
.meals-grid {{
    display:grid; grid-template-columns:repeat(auto-fill, minmax(240px, 1fr));
    gap:12px;
}}
.meal {{
    background:rgba(255,255,255,0.025);
    border-radius:8px; padding:14px 16px;
    border-left:3px solid;
}}
.meal.breakfast {{ border-color:#fbbf24; }}
.meal.lunch {{ border-color:#4ade80; }}
.meal.dinner {{ border-color:#8b9eff; }}
.meal.snack {{ border-color:#f472b6; }}
.meal.other {{ border-color:#9aa3c7; }}
.meal .m-name {{
    display:flex; align-items:center; gap:7px;
    font-size:11px; text-transform:uppercase; letter-spacing:1px;
    color:var(--muted); margin-bottom:6px;
}}
.meal .m-name .ui-icon {{ width:15px; height:15px; color:currentColor; }}
.meal .m-kcal {{ font-size:20px; font-weight:700; }}
.meal .m-kcal .u {{ font-size:11px; color:var(--muted); font-weight:500; }}
.meal .m-macros {{ font-size:11px; color:var(--dim); margin:4px 0 10px; }}
.meal .m-items {{ font-size:12px; color:var(--muted); line-height:1.5; }}

/* Notes */
.notes-composer {{
    background:var(--panel); border:1px solid var(--border);
    border-radius:8px; padding:16px 18px; margin-bottom:16px;
}}
.notes-composer h3 {{ font-size:13px; color:var(--muted); margin-bottom:10px; text-transform:uppercase; letter-spacing:0.8px; }}
.notes-composer textarea {{
    width:100%; min-height:90px;
    background:rgba(0,0,0,0.25); border:1px solid var(--border); border-radius:10px;
    padding:10px 12px; font-family:inherit; font-size:13px; color:var(--text);
    resize:vertical; outline:none; transition:border-color 0.15s;
}}
.notes-composer textarea:focus {{ border-color:var(--border-hi); }}
.notes-composer .row {{ display:flex; gap:8px; margin-top:10px; align-items:center; flex-wrap:wrap; }}
.notes-composer input[type=text] {{
    background:rgba(0,0,0,0.25); border:1px solid var(--border); border-radius:8px;
    padding:6px 10px; color:var(--text); font-family:inherit; font-size:12px;
    outline:none; flex:1; min-width:120px;
}}
.notes-composer input[type=date] {{
    background:rgba(0,0,0,0.25); border:1px solid var(--border); border-radius:8px;
    padding:6px 10px; color:var(--text); font-family:inherit; font-size:12px;
    color-scheme:dark; outline:none;
}}

.note-card {{
    background:var(--panel); border:1px solid var(--border);
    border-radius:8px; padding:16px 18px; margin-bottom:12px;
    position:relative;
}}
.note-card.has-both {{ border-left:3px solid var(--accent-warm); }}
.note-card.has-yazio {{ border-left:3px solid var(--warn); }}
.note-card.has-manual {{ border-left:3px solid var(--accent); }}
.note-card .note-head {{
    display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap;
    gap:10px; margin-bottom:10px;
}}
.note-card .note-date {{ font-size:13px; color:var(--accent); font-weight:600; }}
.note-card .note-src {{ display:flex; gap:6px; }}
.note-card .chip {{
    font-size:10px; padding:2.5px 8px; border-radius:6px;
    text-transform:uppercase; letter-spacing:0.5px;
}}
.note-card .chip.yazio {{ background:rgba(251,191,36,0.15); color:#fde68a; }}
.note-card .chip.manual {{ background:rgba(139,158,255,0.15); color:#c7d0ff; }}
.note-card .note-section {{
    padding:8px 0;
}}
.note-card .note-section + .note-section {{ border-top:1px dashed var(--border); margin-top:8px; padding-top:12px; }}
.note-card .section-label {{ font-size:10px; color:var(--dim); text-transform:uppercase; letter-spacing:0.8px; margin-bottom:6px; }}
.note-card .note-body {{ color:#d0d6f5; line-height:1.55; font-size:13.5px; white-space:pre-wrap; }}
.note-card .note-tags {{ display:flex; flex-wrap:wrap; gap:5px; margin-top:8px; }}
.note-card .tag {{
    background:rgba(139,158,255,0.1); color:#b8c4ff;
    padding:2px 8px; border-radius:6px; font-size:10.5px;
}}
.note-card .manual-entry {{
    display:flex; align-items:flex-start; gap:10px; padding:6px 0;
}}
.note-card .manual-entry + .manual-entry {{
    border-top:1px dashed rgba(148,163,184,0.18); margin-top:6px; padding-top:12px;
}}
.note-card .manual-entry-main {{ flex:1; min-width:0; }}
.note-card .note-delete-btn {{
    opacity:0.55; padding:4px 8px; line-height:1; flex:0 0 auto;
}}
.note-card .manual-entry:hover .note-delete-btn {{ opacity:1; }}
.note-card .note-actions {{ position:absolute; top:12px; right:14px; display:flex; gap:4px; opacity:0; transition:opacity 0.15s; }}
.note-card:hover .note-actions {{ opacity:1; }}

/* Modal */
.modal-backdrop {{
    position:fixed; inset:0; background:rgba(0,0,0,0.75); backdrop-filter:blur(10px);
    display:none; align-items:flex-start; justify-content:center;
    padding:40px 20px; z-index:100; overflow-y:auto;
}}
.modal-backdrop.open {{ display:flex; }}
.modal {{
    background:var(--bg2); border:1px solid var(--border);
    border-radius:8px; padding:28px 32px;
    max-width:820px; width:100%;
    position:relative;
}}
.modal h2 {{
    font-size:20px; margin-bottom:6px;
    display:flex; align-items:center; gap:10px;
}}
.modal .modal-meta {{ color:var(--dim); font-size:12px; margin-bottom:20px; }}
.modal .modal-close {{
    position:absolute; top:16px; right:18px;
    background:transparent; border:1px solid var(--border); color:var(--muted);
    width:34px; height:34px; border-radius:50%; cursor:pointer; font-size:18px;
    display:flex; align-items:center; justify-content:center;
}}
.modal .modal-close:hover {{ color:var(--text); border-color:var(--border-hi); }}

/* Symptom acute — discreet, no alarm-color */
.symptom-link {{
    display:inline-flex; align-items:center; gap:6px;
    color:var(--accent2); font-size:12.5px; cursor:pointer;
    background:transparent; border:1px dashed var(--border);
    padding:6px 12px; border-radius:6px; transition:all 0.15s;
    text-decoration:none;
}}
.symptom-link:hover {{ border-color:var(--accent); color:var(--accent); border-style:solid; }}
.symptom-link .ico {{ opacity:0.7; }}
.symptom-input {{
    width:100%; padding:11px 14px; border-radius:8px;
    background:var(--bg); border:1px solid var(--border); color:var(--text);
    font-size:14px; font-family:inherit; box-sizing:border-box;
}}
.symptom-input:focus {{ outline:none; border-color:var(--accent); }}
.symptom-quick {{ display:flex; flex-wrap:wrap; gap:6px; margin:10px 0 14px; }}
.symptom-quick .btn {{ font-size:12px; padding:6px 11px; }}
.symptom-quick .btn.go {{ margin-left:auto; font-weight:500; }}
.symptom-loading {{ color:var(--dim); font-style:italic; padding:20px 0; }}
.symptom-meta {{
    color:var(--dim); font-size:11px; margin-top:14px;
    padding-top:10px; border-top:1px solid var(--border);
}}

/* AI typography inside cards/modals */
.ai-content {{ line-height:1.65; color:#d8deff; font-size:14px; }}
.ai-content .ai-summary {{
    background:linear-gradient(135deg, rgba(139,158,255,0.12), rgba(201,158,255,0.08));
    padding:14px 18px; border-radius:8px; margin-bottom:20px;
    font-size:15px; font-weight:500; color:#f0f4ff;
    border-left:3px solid var(--accent);
}}
.ai-content h3 {{
    font-size:14px; margin:18px 0 8px; color:var(--accent2);
    text-transform:uppercase; letter-spacing:0.8px; font-weight:600;
}}
.ai-content p {{ margin-bottom:10px; }}
.ai-content ul {{ padding-left:22px; margin-bottom:10px; }}
.ai-content li {{ margin-bottom:6px; }}
.ai-content strong {{ color:var(--text); }}
.ai-content em {{ color:var(--accent-warm); font-style:normal; }}

/* Compact per-tab AI block (вверху каждой категории) */
.category-ai {{
    background:linear-gradient(135deg, rgba(139,158,255,0.07), rgba(201,158,255,0.04));
    border:1px solid var(--border);
    border-left:3px solid var(--accent2);
    border-radius:8px;
    padding:14px 18px 12px;
    margin-bottom:14px;
}}
.category-ai .cat-ai-head {{
    display:flex; justify-content:space-between; align-items:center; gap:12px;
    margin-bottom:8px;
}}
.category-ai .cat-ai-title {{
    font-size:11px; text-transform:uppercase; letter-spacing:1px;
    color:var(--muted); display:flex; align-items:center; gap:8px;
}}
.category-ai .cat-ai-title .icon {{ font-size:14px; }}
.category-ai .cat-ai-actions {{ display:flex; gap:6px; align-items:center; }}
.category-ai .cat-ai-actions .meta {{ color:var(--dim); font-size:10.5px; }}
.category-ai .cat-ai-body {{
    font-size:13px; line-height:1.55; color:#d8deff;
}}
.category-ai .cat-ai-body .ai-summary {{
    background:transparent; padding:0; margin:0 0 8px 0;
    border:none; font-weight:500; color:#f0f4ff; font-size:14px;
}}
.category-ai .cat-ai-body h3 {{
    font-size:11.5px; margin:10px 0 4px; color:var(--accent2);
    text-transform:uppercase; letter-spacing:0.8px; font-weight:600;
}}
.category-ai .cat-ai-body p {{ margin-bottom:6px; }}
.category-ai .cat-ai-body ul {{ padding-left:20px; margin-bottom:4px; }}
.category-ai .cat-ai-body li {{ margin-bottom:3px; }}
.category-ai .cat-ai-body em {{ color:var(--accent-warm); font-style:normal; }}
.category-ai.empty {{ border-left-color:var(--dim); }}
.category-ai.empty .cat-ai-body {{ color:var(--dim); font-style:italic; }}

/* Food ideas */
.food-ideas {{
    display:grid; grid-template-columns:repeat(auto-fill, minmax(240px, 1fr));
    gap:10px;
}}
.food-idea {{
    background:rgba(255,255,255,0.025);
    border:1px solid var(--border);
    border-radius:8px;
    padding:10px 14px;
}}
.food-idea .nm {{ font-size:13px; color:var(--text); font-weight:650; margin-bottom:3px; }}
.food-idea .meta {{ font-size:11px; color:var(--accent2); margin-bottom:6px; text-transform:uppercase; letter-spacing:0.5px; }}
.food-idea .why {{ font-size:12px; color:var(--muted); line-height:1.45; margin-bottom:6px; }}
.food-idea .cook {{ font-size:12.5px; color:#dfe9df; line-height:1.45; }}
.food-idea .recipe {{ margin-top:8px; padding-top:8px; border-top:1px dashed var(--border); color:#cbd8ca; font-size:12px; line-height:1.45; }}

.food-profile {{
    display:grid; grid-template-columns:1.1fr 0.9fr; gap:14px;
}}
.profile-card {{
    background:var(--panel); border:1px solid var(--border); border-radius:8px;
    padding:16px 18px;
}}
.profile-card h2 {{ font-size:14px; margin-bottom:8px; color:var(--text); }}
.profile-card .hint {{ color:var(--muted); font-size:12.5px; line-height:1.5; margin-bottom:12px; }}
.profile-grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:8px; }}
.chip-check {{
    display:flex; align-items:center; gap:8px;
    background:rgba(255,255,255,0.025); border:1px solid var(--border);
    border-radius:7px; padding:8px 10px; color:var(--text); font-size:12.5px;
}}
.chip-check input {{ accent-color:var(--accent); }}
.profile-card textarea {{
    width:100%; min-height:90px; background:rgba(0,0,0,0.22);
    border:1px solid var(--border); border-radius:7px; color:var(--text);
    padding:10px 12px; font-family:inherit; resize:vertical;
}}
.profile-actions {{ display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; }}
.profile-kv {{ display:grid; gap:8px; color:var(--muted); font-size:12.5px; line-height:1.45; }}
.profile-kv strong {{ color:var(--text); }}

.workout-list {{
    display:grid; gap:8px; margin-top:10px;
}}
.workout-controls {{
    display:flex; align-items:center; justify-content:space-between; gap:10px;
    flex-wrap:wrap; margin:0 0 10px;
}}
.workout-filter {{
    display:flex; gap:6px; flex-wrap:wrap;
}}
.workout-filter .btn.active {{
    border-color:rgba(125,211,199,0.48);
    background:rgba(125,211,199,0.12);
    color:#dffcf7;
}}
.workout-count {{
    color:var(--dim);
    font-size:11px;
}}
.workout-card {{
    background:rgba(255,255,255,0.025); border:1px solid var(--border);
    border-radius:8px; padding:10px 12px;
}}
.workout-card .workout-row {{
    display:grid; grid-template-columns:minmax(0, 1.4fr) repeat(auto-fit, minmax(72px, 0.4fr));
    gap:10px; align-items:center;
}}
.workout-card .name {{ color:var(--text); font-weight:650; font-size:13px; }}
.workout-card .src {{ color:var(--dim); font-size:11px; margin-top:2px; }}
.workout-card .metric {{ color:var(--muted); font-size:11px; text-align:right; }}
.workout-card .metric strong {{ display:block; color:var(--accent); font-size:13px; }}
.workout-card .ks-toggle {{ cursor:pointer; color:var(--accent); user-select:none; }}
.workout-card .ks-toggle:hover strong {{ filter:brightness(1.2); }}
.workout-card .workout-share-btn {{
    justify-self:end;
    color:#dbeafe;
    border-color:rgba(139,158,255,0.28);
    background:rgba(139,158,255,0.08);
}}
.workout-card .workout-share-btn:hover {{
    background:rgba(139,158,255,0.16);
    border-color:rgba(139,158,255,0.52);
}}
.ks-chart-wrap {{
    margin-top:10px; padding:8px;
    background:rgba(255,255,255,0.02); border:1px solid var(--border); border-radius:6px;
    height:220px; position:relative;
}}
.ks-chart-wrap canvas {{ height:100% !important; width:100% !important; }}
.zone-bar {{
    display:flex; height:8px; margin-top:8px; border-radius:4px; overflow:hidden;
    background:rgba(255,255,255,0.04);
}}
.zone-seg {{ height:100%; }}

/* Workout share card */
.workout-share-modal {{ max-width:560px; }}
.share-card {{
    overflow:hidden;
    border-radius:18px;
    background:#eef5ff;
    color:#0b2f67;
    box-shadow:0 22px 70px rgba(0,0,0,0.38);
}}
.share-map {{
    position:relative;
    height:330px;
    background:#dfe9f6;
}}
.share-map canvas {{
    width:100%;
    height:100%;
    display:block;
}}
.share-brand {{
    position:absolute;
    top:18px;
    left:18px;
    font-weight:750;
    font-size:13px;
    color:#0d56c2;
    text-shadow:0 1px 0 rgba(255,255,255,0.55);
}}
.share-map-badge {{
    position:absolute;
    right:14px;
    bottom:14px;
    max-width:58%;
    padding:7px 10px;
    border-radius:999px;
    background:rgba(255,255,255,0.86);
    color:#315174;
    font-size:11px;
    font-weight:650;
    box-shadow:0 8px 24px rgba(55,82,120,0.16);
}}
.share-card-body {{
    padding:22px 26px 26px;
    background:#f5f8fd;
}}
.share-title {{
    font-size:24px;
    font-weight:800;
    letter-spacing:-0.02em;
    color:#153d76;
}}
.share-time {{
    color:#667896;
    font-size:13px;
    margin-top:6px;
}}
.share-stats {{
    display:grid;
    grid-template-columns:repeat(3, minmax(0, 1fr));
    gap:14px;
    margin-top:24px;
}}
.share-stat {{
    text-align:center;
}}
.share-stat .num {{
    display:block;
    color:#0966d8;
    font-size:25px;
    font-weight:850;
    letter-spacing:-0.03em;
}}
.share-stat .lbl {{
    display:block;
    margin-top:3px;
    color:#5d7191;
    font-size:12px;
}}
.share-actions {{
    display:flex;
    gap:8px;
    align-items:center;
    justify-content:space-between;
    flex-wrap:wrap;
    margin-top:14px;
}}
.share-note {{
    color:var(--dim);
    font-size:11.5px;
    line-height:1.4;
    max-width:330px;
}}

.notes-status {{
    background:linear-gradient(135deg, rgba(125,211,199,0.09), rgba(245,195,107,0.06));
    border:1px solid var(--border);
    border-left:3px solid var(--accent);
    border-radius:8px;
    padding:14px 16px;
    margin-bottom:14px;
}}
.notes-status .label {{ color:var(--muted); font-size:10.5px; text-transform:uppercase; letter-spacing:0.8px; margin-bottom:5px; }}
.notes-status .msg {{ color:var(--text); font-size:13.5px; line-height:1.45; margin-bottom:6px; }}
.notes-status .meta {{ color:var(--dim); font-size:11.5px; }}

/* Toast */
.toast {{
    position:fixed; bottom:20px; right:20px;
    background:var(--bg2); border:1px solid var(--border-hi);
    padding:12px 18px; border-radius:12px; font-size:13px;
    display:none; z-index:200; max-width:340px;
    box-shadow:0 10px 40px rgba(0,0,0,0.5);
}}
.toast.show {{ display:block; animation:slideUp 0.2s ease; }}
.toast.ok {{ border-left:3px solid var(--ok); }}
.toast.err {{ border-left:3px solid var(--bad); }}
@keyframes slideUp {{ from {{ transform:translateY(12px); opacity:0; }} to {{ transform:translateY(0); opacity:1; }} }}

footer {{ text-align:center; color:var(--dim); font-size:11px; margin-top:32px; padding:16px; }}

.empty {{ text-align:center; color:var(--dim); padding:40px; }}

/* Scrollbar */
::-webkit-scrollbar {{ width:10px; height:10px; }}
::-webkit-scrollbar-track {{ background:transparent; }}
::-webkit-scrollbar-thumb {{ background:rgba(255,255,255,0.08); border-radius:5px; }}
::-webkit-scrollbar-thumb:hover {{ background:rgba(255,255,255,0.15); }}

@media (max-width: 760px) {{
    body {{ padding:12px 10px 36px; }}
    header {{ grid-template-columns:1fr; gap:12px; padding:14px; }}
    .head-controls {{ justify-content:space-between; }}
    .ai-card {{ grid-template-columns:1fr; gap:10px; padding:14px; }}
    .ai-card .ai-controls {{ flex-wrap:wrap; }}
    .period-switch {{ max-width:100%; overflow-x:auto; }}
    .stats {{ grid-template-columns:repeat(2, minmax(0, 1fr)); gap:10px; }}
    .stat {{ padding:12px; min-width:0; }}
    .stat .value {{ font-size:20px; }}
    .tabs {{ gap:3px; }}
    .tab {{ padding:8px 11px; }}
    .charts-grid {{ grid-template-columns:minmax(0, 1fr); }}
    .chart-card {{ padding:14px; min-width:0; }}
    .chart-wrapper {{ height:230px; }}
    .meals-grid, .food-ideas {{ grid-template-columns:minmax(0, 1fr); }}
    .food-profile {{ grid-template-columns:minmax(0, 1fr); }}
    .category-ai .cat-ai-head {{ align-items:flex-start; flex-direction:column; }}
    .category-ai .cat-ai-actions {{ flex-wrap:wrap; }}
    .modal-backdrop {{ padding:16px 10px; }}
    .modal {{ padding:22px 16px; }}
    .share-map {{ height:270px; }}
    .share-card-body {{ padding:18px 18px 22px; }}
    .share-title {{ font-size:21px; }}
    .share-stats {{ gap:8px; }}
    .share-stat .num {{ font-size:21px; }}
}}

/* --- AI Chat Widget --- */
.chat-fab {{
    position:fixed; bottom:24px; right:24px; z-index:9000;
    width:56px; height:56px; border-radius:50%;
    background:linear-gradient(135deg, var(--accent), #5bb8aa);
    border:none; cursor:pointer;
    box-shadow:0 4px 20px rgba(125,211,199,0.35);
    display:flex; align-items:center; justify-content:center;
    font-size:24px; color:#090d10;
    transition:all 0.25s;
}}
.chat-fab:hover {{ transform:scale(1.1); box-shadow:0 6px 28px rgba(125,211,199,0.5); }}
.chat-fab.has-unread {{ animation:chat-pulse 2s infinite; }}
@keyframes chat-pulse {{
    0%,100% {{ box-shadow:0 4px 20px rgba(125,211,199,0.35); }}
    50% {{ box-shadow:0 4px 28px rgba(125,211,199,0.65); }}
}}

.chat-panel {{
    position:fixed; bottom:90px; right:24px; z-index:9001;
    width:420px; max-height:70vh;
    background:var(--bg1);
    border:1px solid var(--border-hi);
    border-radius:16px;
    box-shadow:0 12px 48px rgba(0,0,0,0.6);
    display:flex; flex-direction:column;
    opacity:0; transform:translateY(20px) scale(0.95);
    pointer-events:none;
    transition:all 0.25s cubic-bezier(.4,0,.2,1);
    backdrop-filter:blur(24px);
}}
.chat-panel.open {{
    opacity:1; transform:translateY(0) scale(1);
    pointer-events:auto;
}}
.chat-header {{
    display:flex; align-items:center; justify-content:space-between;
    padding:14px 18px;
    border-bottom:1px solid var(--border);
    background:rgba(125,211,199,0.06);
    border-radius:16px 16px 0 0;
}}
.chat-header .chat-title {{
    font-size:14px; font-weight:600; color:var(--accent);
    display:flex; align-items:center; gap:8px;
}}
.chat-header .chat-close {{
    background:none; border:none; color:var(--muted);
    cursor:pointer; font-size:18px; padding:4px;
    transition:color 0.15s;
}}
.chat-header .chat-close:hover {{ color:var(--text); }}
.chat-messages {{
    flex:1; overflow-y:auto; padding:16px;
    display:flex; flex-direction:column; gap:12px;
    min-height:200px; max-height:calc(70vh - 140px);
}}
.chat-messages::-webkit-scrollbar {{ width:4px; }}
.chat-messages::-webkit-scrollbar-thumb {{ background:var(--border); border-radius:4px; }}

.chat-msg {{
    max-width:88%; padding:10px 14px;
    border-radius:12px; font-size:13px; line-height:1.55;
    animation:chat-msg-in 0.25s ease-out;
}}
@keyframes chat-msg-in {{
    from {{ opacity:0; transform:translateY(8px); }}
    to {{ opacity:1; transform:translateY(0); }}
}}
.chat-msg.user {{
    align-self:flex-end;
    background:linear-gradient(135deg, rgba(125,211,199,0.2), rgba(125,211,199,0.1));
    border:1px solid rgba(125,211,199,0.25);
    color:var(--text);
}}
.chat-msg.ai {{
    align-self:flex-start;
    background:var(--panel);
    border:1px solid var(--border);
    color:var(--text);
}}
.chat-msg.ai p {{ margin:6px 0; }}
.chat-msg.ai ul {{ margin:6px 0; padding-left:18px; }}
.chat-msg.ai li {{ margin:3px 0; }}
.chat-msg.ai strong {{ color:var(--accent); }}
.chat-msg.system {{
    align-self:center;
    color:var(--muted); font-size:12px;
    font-style:italic; padding:4px 10px;
}}
.chat-typing {{
    display:flex; gap:4px; align-items:center;
    padding:10px 14px;
    align-self:flex-start;
}}
.chat-typing span {{
    width:6px; height:6px; border-radius:50%;
    background:var(--accent); opacity:0.4;
    animation:typing-bounce 1.2s infinite;
}}
.chat-typing span:nth-child(2) {{ animation-delay:0.2s; }}
.chat-typing span:nth-child(3) {{ animation-delay:0.4s; }}
@keyframes typing-bounce {{
    0%,60%,100% {{ transform:translateY(0); opacity:0.4; }}
    30% {{ transform:translateY(-6px); opacity:1; }}
}}

.chat-input-area {{
    display:flex; gap:8px; padding:12px 14px;
    border-top:1px solid var(--border);
    background:rgba(0,0,0,0.15);
    border-radius:0 0 16px 16px;
}}
.chat-input-area textarea {{
    flex:1; resize:none;
    background:var(--panel);
    border:1px solid var(--border);
    border-radius:10px;
    color:var(--text); font-family:inherit;
    font-size:13px; padding:10px 12px;
    outline:none;
    transition:border-color 0.15s;
    min-height:40px; max-height:100px;
}}
.chat-input-area textarea:focus {{ border-color:var(--accent); }}
.chat-input-area textarea::placeholder {{ color:var(--dim); }}
.chat-send {{
    width:40px; height:40px; border-radius:10px;
    background:linear-gradient(135deg, var(--accent), #5bb8aa);
    border:none; cursor:pointer;
    color:#090d10; font-size:16px;
    display:flex; align-items:center; justify-content:center;
    transition:all 0.15s; align-self:flex-end;
}}
.chat-send:hover {{ transform:scale(1.05); }}
.chat-send:disabled {{ opacity:0.4; cursor:not-allowed; transform:none; }}

.chat-suggestions {{
    display:flex; flex-wrap:wrap; gap:6px;
    padding:0 14px 10px;
}}
.chat-chip {{
    padding:6px 12px; font-size:11px;
    background:var(--panel); border:1px solid var(--border);
    border-radius:20px; cursor:pointer;
    color:var(--muted); transition:all 0.15s;
    font-family:inherit;
}}
.chat-chip:hover {{
    border-color:var(--accent); color:var(--accent);
    background:rgba(125,211,199,0.08);
}}

@media(max-width:500px) {{
    .chat-panel {{ width:calc(100vw - 20px); right:10px; bottom:80px; max-height:75vh; }}
    .chat-fab {{ bottom:16px; right:16px; width:50px; height:50px; font-size:22px; }}
}}

/* Health Dashboard V2 */
:root {{
    --bg0:#060b15;
    --bg1:#091221;
    --bg2:#0d1828;
    --bg:var(--bg1);
    --panel:rgba(14, 27, 45, 0.78);
    --panel-hi:rgba(20, 38, 62, 0.9);
    --panel-solid:#0d1929;
    --border:rgba(143, 169, 207, 0.14);
    --border-hi:rgba(140, 124, 255, 0.52);
    --text:#f3f6fc;
    --muted:#a6b1c4;
    --dim:#6f7d94;
    --accent:#8c7cff;
    --accent-rgb:140,124,255;
    --accent2:#55d8ff;
    --accent-warm:#ff9a62;
    --ok:#45d99a;
    --warn:#ffbd55;
    --bad:#ff6f86;
    --radius-sm:10px;
    --radius-md:14px;
    --radius-lg:18px;
    --shadow-card:0 18px 48px rgba(0,0,0,.18);
}}
html {{ color-scheme:dark; }}
html,body {{
    font-family:'Manrope','Segoe UI',sans-serif;
    background:#060b15;
}}
body {{
    padding:0;
    overflow-x:hidden;
    background:
        radial-gradient(circle at 16% -8%, rgba(60,90,180,.16), transparent 32%),
        radial-gradient(circle at 92% 4%, rgba(37,186,220,.08), transparent 24%),
        linear-gradient(180deg,#060b15 0%,#08111e 48%,#060b15 100%);
}}
body:before {{
    content:"";
    position:fixed;
    inset:0;
    pointer-events:none;
    opacity:.18;
    background-image:linear-gradient(rgba(255,255,255,.018) 1px,transparent 1px),
                     linear-gradient(90deg,rgba(255,255,255,.014) 1px,transparent 1px);
    background-size:48px 48px;
    mask-image:linear-gradient(to bottom,black,transparent 82%);
}}
.icon-sprite {{ position:absolute; width:0; height:0; overflow:hidden; }}
.ui-icon {{
    width:20px;
    height:20px;
    flex:0 0 auto;
    fill:none;
    stroke:currentColor;
    stroke-width:2;
    stroke-linecap:round;
    stroke-linejoin:round;
}}
.app-shell {{
    width:min(100%, 1920px);
    margin:0 auto;
    min-height:100vh;
    display:grid;
    grid-template-columns:104px minmax(0,1fr);
    position:relative;
    z-index:1;
}}
.sidebar {{
    position:sticky;
    top:0;
    height:100vh;
    padding:18px 12px;
    border-right:1px solid var(--border);
    background:rgba(6,13,24,.82);
    backdrop-filter:blur(22px);
    display:flex;
    flex-direction:column;
    gap:18px;
    z-index:30;
}}
.side-logo {{
    width:56px;
    height:56px;
    margin:0 auto 4px;
    border-radius:17px;
    display:grid;
    place-items:center;
    color:#b9b1ff;
    background:linear-gradient(145deg,rgba(140,124,255,.24),rgba(58,86,137,.12));
    border:1px solid rgba(156,146,255,.28);
    box-shadow:0 16px 34px rgba(39,36,104,.28),inset 0 1px rgba(255,255,255,.08);
}}
.side-logo .ui-icon {{ width:28px; height:28px; }}
.sidebar .tabs {{
    margin:0;
    padding:0;
    background:transparent;
    border:0;
    border-radius:0;
    overflow:visible;
    display:flex;
    flex-direction:column;
    gap:7px;
}}
.sidebar .tab {{
    min-height:62px;
    width:100%;
    padding:8px 5px;
    border:1px solid transparent;
    border-radius:14px;
    display:flex;
    flex-direction:column;
    justify-content:center;
    gap:6px;
    color:#7f8ba0;
    font-size:10.5px;
    font-weight:600;
    line-height:1.1;
}}
.sidebar .tab .ui-icon {{ width:22px; height:22px; }}
.sidebar .tab:hover {{
    color:#d9e2f2;
    background:rgba(255,255,255,.035);
}}
.sidebar .tab.active {{
    color:var(--section-accent,#a99fff);
    border-color:color-mix(in srgb,var(--section-accent,#8c7cff) 38%,transparent);
    background:linear-gradient(145deg,
        color-mix(in srgb,var(--section-accent,#8c7cff) 18%,transparent),
        rgba(255,255,255,.018));
    box-shadow:inset 3px 0 var(--section-accent,#8c7cff);
}}
.sidebar-foot {{
    margin-top:auto;
    text-align:center;
    color:#58667c;
    font:600 9px/1.4 'IBM Plex Mono',monospace;
    letter-spacing:.08em;
}}
.dashboard-main {{ min-width:0; }}
.container {{
    max-width:1680px;
    margin:0 auto;
    padding:20px 22px 60px;
}}
header {{
    min-height:68px;
    margin-bottom:14px;
    padding:8px 4px 14px;
    border:0;
    border-bottom:1px solid var(--border);
    border-radius:0;
    background:transparent;
    backdrop-filter:none;
}}
.brand h1 {{
    color:var(--text);
    background:none;
    -webkit-text-fill-color:initial;
    font-size:25px;
    font-weight:800;
    letter-spacing:-.7px;
}}
.brand .sub {{ margin-top:5px; font-size:12px; color:#8794aa; }}
.brand-kicker {{
    margin-bottom:3px;
    color:var(--accent2);
    font:600 9px/1 'IBM Plex Mono',monospace;
    letter-spacing:.15em;
    text-transform:uppercase;
}}
.head-controls {{ gap:9px; }}
.head-controls .sync-info {{
    padding-right:3px;
    color:#75839a;
    font-size:10.5px;
    line-height:1.45;
}}
.sync-notice.warn {{
    justify-self:start;
    width:max-content;
    max-width:100%;
    margin-top:3px;
    padding:6px 10px;
    border:1px solid rgba(255,189,85,.24);
    border-radius:999px;
    background:rgba(255,189,85,.07);
    font-size:10.5px;
}}
.sync-dot {{
    display:inline-block;
    width:7px;
    height:7px;
    margin-left:5px;
    border-radius:50%;
    background:var(--ok);
    box-shadow:0 0 12px rgba(69,217,154,.65);
}}
.data-health-btn {{
    min-width:94px;
    justify-content:center;
}}
.data-health-dot {{
    width:8px;
    height:8px;
    border-radius:50%;
    background:var(--dim);
    box-shadow:0 0 0 3px rgba(111,125,148,.12);
}}
.data-health-btn[data-health-state="fresh"] .data-health-dot {{
    background:var(--ok);
    box-shadow:0 0 12px rgba(69,217,154,.55);
}}
.data-health-btn[data-health-state="attention"] .data-health-dot {{
    background:var(--warn);
    box-shadow:0 0 12px rgba(251,191,36,.45);
}}
.data-health-grid {{
    display:grid;
    gap:8px;
}}
.data-health-row {{
    display:grid;
    grid-template-columns:minmax(0,1fr) auto;
    gap:12px;
    align-items:center;
    padding:11px 12px;
    border:1px solid var(--border);
    border-radius:12px;
    background:rgba(255,255,255,.025);
}}
.data-health-name {{
    display:flex;
    align-items:center;
    gap:9px;
    font-weight:700;
    font-size:13px;
}}
.source-state-dot {{
    width:8px;
    height:8px;
    border-radius:50%;
    background:var(--dim);
}}
.source-state-dot.fresh {{ background:var(--ok); }}
.source-state-dot.cached {{ background:#60a5fa; }}
.source-state-dot.stale {{ background:var(--warn); }}
.source-state-dot.missing {{ background:var(--bad); }}
.data-health-detail {{
    margin-top:4px;
    color:var(--dim);
    font-size:10.5px;
}}
.data-health-state {{
    color:var(--muted);
    font:600 10px/1 'IBM Plex Mono',monospace;
    text-transform:uppercase;
}}
.data-health-jobs {{
    margin-top:14px;
    padding-top:14px;
    border-top:1px solid var(--border);
    color:var(--muted);
    font-size:12px;
}}
.btn {{
    min-height:40px;
    padding:9px 14px;
    border-radius:11px;
    border-color:var(--border);
    background:rgba(16,30,49,.76);
    font-weight:650;
    transition:background .2s ease,border-color .2s ease,color .2s ease,transform .2s ease;
}}
.btn:hover {{
    background:rgba(24,43,69,.92);
    border-color:rgba(var(--accent-rgb),.46);
}}
.btn:focus-visible,.tab:focus-visible,.period-switch button:focus-visible,
input:focus-visible,textarea:focus-visible,summary:focus-visible {{
    outline:2px solid color-mix(in srgb,var(--section-accent,var(--accent)) 78%,white);
    outline-offset:2px;
}}
.btn.icon {{ width:42px; padding:0; justify-content:center; }}
.btn.icon .ui-icon {{ width:18px; height:18px; }}
.btn.primary {{
    background:linear-gradient(135deg,rgba(var(--accent-rgb),.28),rgba(75,112,205,.18));
    border-color:rgba(var(--accent-rgb),.45);
}}
.period-switch {{
    padding:3px;
    border-radius:12px;
    background:rgba(8,17,30,.72);
}}
.period-switch button {{
    min-height:34px;
    padding:7px 13px;
    border-radius:9px;
    font-weight:650;
    display:inline-flex;
    align-items:center;
    gap:6px;
}}
.period-switch button .ui-icon {{ width:15px; height:15px; }}
.period-switch button.active {{
    color:#fff;
    background:linear-gradient(135deg,rgba(var(--accent-rgb),.48),rgba(var(--accent-rgb),.24));
    box-shadow:0 6px 18px rgba(var(--accent-rgb),.14);
}}
.ai-card,.category-ai,.chart-card,.stat,.notes-composer,.note-card,
.profile-card,.notes-status {{
    border:1px solid var(--border);
    background:
        linear-gradient(145deg,rgba(18,34,56,.9),rgba(10,21,36,.88));
    box-shadow:var(--shadow-card);
}}
.ai-card {{
    min-height:92px;
    margin-bottom:14px;
    padding:18px 20px;
    border-radius:var(--radius-lg);
    border-left:1px solid rgba(var(--accent-rgb),.46);
    background:
        radial-gradient(circle at 12% 50%,rgba(var(--accent-rgb),.16),transparent 31%),
        linear-gradient(110deg,rgba(17,31,54,.95),rgba(9,20,36,.92));
}}
.ai-card .ai-icon {{
    width:48px;
    height:48px;
    border-radius:15px;
    display:grid;
    place-items:center;
    color:#bdb6ff;
    background:rgba(var(--accent-rgb),.14);
    border:1px solid rgba(var(--accent-rgb),.22);
}}
.ai-card .ai-icon .ui-icon {{ width:25px; height:25px; }}
.ai-card .ai-summary-line {{ font-size:15px; font-weight:650; }}
.ai-card .ai-meta {{
    margin-top:6px;
    font:500 10px/1.2 'IBM Plex Mono',monospace;
    letter-spacing:.03em;
}}
.stats {{
    grid-template-columns:repeat(4,minmax(150px,1fr));
    gap:10px;
    margin-bottom:16px;
}}
.stat {{
    min-height:104px;
    padding:14px;
    border-radius:var(--radius-md);
    position:relative;
    overflow:hidden;
}}
.stat:after {{
    content:"";
    position:absolute;
    right:-34px;
    top:-34px;
    width:86px;
    height:86px;
    border-radius:50%;
    background:color-mix(in srgb,var(--stat-color,var(--accent)) 10%,transparent);
}}
.stat:hover {{ transform:translateY(-2px); border-color:color-mix(in srgb,var(--stat-color,var(--accent)) 38%,transparent); }}
.stat-head {{ display:flex; align-items:center; gap:7px; margin-bottom:9px; }}
.stat-icon {{
    color:var(--stat-color,var(--accent));
    width:18px;
    height:18px;
}}
.stat .label {{
    margin:0;
    font-size:9px;
    font-weight:700;
    letter-spacing:.08em;
}}
.stat .value {{ font-size:23px; font-weight:800; }}
.stat .delta {{ margin-top:5px; line-height:1.3; }}
.category-ai {{
    --section-accent:var(--accent);
    padding:16px 18px;
    margin-bottom:12px;
    border-radius:var(--radius-lg);
    border-left:2px solid var(--section-accent);
    background:
        linear-gradient(100deg,color-mix(in srgb,var(--section-accent) 9%,#0d1929),rgba(10,21,36,.92));
}}
.category-ai .cat-ai-title {{ color:var(--text); font-size:13px; }}
.category-ai .cat-ai-body {{
    margin-top:11px;
    padding-top:12px;
    border-top:1px solid rgba(255,255,255,.055);
    color:#bdc7d8;
    max-height:176px;
    overflow:hidden;
    mask-image:linear-gradient(to bottom,#000 0%,#000 78%,transparent 100%);
}}
.tab-panel {{ --section-accent:#8c7cff; animation:panel-in .28s ease-out both; }}
#tab-sleep {{ --section-accent:#8c7cff; }}
#tab-body {{ --section-accent:#2dd4d0; }}
#tab-nutrition {{ --section-accent:#ff934d; }}
#tab-foodprofile {{ --section-accent:#45d99a; }}
#tab-activity {{ --section-accent:#3f8cff; }}
#tab-health {{ --section-accent:#ff648a; }}
#tab-notes {{ --section-accent:#e8ad42; }}
.charts-grid {{ grid-template-columns:repeat(12,minmax(0,1fr)); gap:12px; }}
.chart-card {{
    grid-column:span 6;
    min-width:0;
    padding:18px;
    border-radius:var(--radius-lg);
}}
.chart-card.wide {{ grid-column:1/-1; }}
#tab-sleep .sleep-phases {{ grid-column:span 8; }}
#tab-sleep .sleep-latest {{ grid-column:span 4; }}
#tab-sleep .sleep-hr {{ grid-column:1/-1; }}
#tab-health .health-overview {{ grid-column:1/-1; }}
#tab-health .health-stress,
#tab-health .health-spo2 {{ grid-column:span 6; }}
.chart-card h2 {{
    margin-bottom:14px;
    font-size:14px;
    font-weight:750;
    letter-spacing:-.15px;
}}
.chart-card h2 .icon {{
    width:30px;
    height:30px;
    border-radius:9px;
    display:grid;
    place-items:center;
    color:var(--section-accent);
    background:color-mix(in srgb,var(--section-accent) 11%,transparent);
}}
.chart-card h2 .icon .ui-icon {{ width:17px; height:17px; }}
.chart-card h2 .hint {{
    font:500 9.5px/1 'IBM Plex Mono',monospace;
    text-transform:uppercase;
    letter-spacing:.06em;
}}
.chart-wrapper {{ height:300px; }}
.food-profile {{ gap:12px; }}
.profile-card {{ border-radius:var(--radius-lg); }}
.meal,.diary-rollup,.nutrition-day,.energy-card {{
    border-radius:var(--radius-sm);
    background:rgba(255,255,255,.025);
}}
.notes-composer,.note-card,.notes-status {{ border-radius:var(--radius-lg); }}
footer {{
    margin-top:24px;
    border-top:1px solid var(--border);
    padding-top:20px;
    font:500 9px/1.6 'IBM Plex Mono',monospace;
    letter-spacing:.03em;
}}

/* V3 composition: every section follows the same 12-column rhythm. */
.app-shell {{ grid-template-columns:88px minmax(0,1fr); }}
.sidebar {{ padding:16px 10px; }}
.side-logo {{
    width:48px;
    height:48px;
    border-radius:14px;
}}
.side-logo .ui-icon {{ width:25px; height:25px; }}
.sidebar .tabs {{ gap:5px; }}
.sidebar .tab {{
    min-height:54px;
    padding:7px 4px;
    border-radius:12px;
    gap:5px;
    font-size:9.5px;
}}
.sidebar .tab .ui-icon {{ width:20px; height:20px; }}
.sidebar .tab.active {{
    box-shadow:inset 2px 0 var(--section-accent,#8c7cff);
}}
.container {{
    max-width:1500px;
    padding:18px 18px 56px;
}}
.ai-card {{ min-height:78px; }}
.category-ai.section-insight {{
    margin:0;
    min-width:0;
    height:auto;
    min-height:0;
    display:flex;
    flex-direction:column;
}}
.category-ai.section-insight .cat-ai-head {{
    display:grid;
    grid-template-columns:minmax(0,1fr);
    gap:8px;
    align-items:start;
}}
.category-ai.section-insight .cat-ai-title {{
    font-size:12px;
    line-height:1.35;
}}
.category-ai.section-insight .cat-ai-actions {{
    width:100%;
    display:grid;
    grid-template-columns:minmax(0,1fr) auto auto;
    gap:7px;
}}
.category-ai.section-insight .cat-ai-actions .meta {{
    min-width:0;
    overflow:hidden;
    text-overflow:ellipsis;
    white-space:nowrap;
    align-self:center;
}}
.category-ai.section-insight .cat-ai-body {{
    flex:0 0 auto;
    max-height:none;
    overflow:visible;
}}
.chart-card {{
    height:382px;
    margin:0;
    overflow:hidden;
}}
.chart-card .chart-wrapper {{ height:304px; }}
.chart-card.body-measurements {{
    overflow:auto;
    scrollbar-width:thin;
}}
.chart-card.nutrition-ideas {{
    overflow:auto;
    scrollbar-width:thin;
}}
#tab-nutrition .nutrition-diary-card,
#tab-nutrition .nutrition-action-panel {{
    height:auto;
    min-height:0;
}}
#tab-nutrition .nutrition-action-panel {{
    overflow:hidden;
}}
#tab-nutrition .nutrition-action-panel .food-ideas {{
    grid-template-columns:minmax(0,1fr);
    align-content:start;
}}
#tab-nutrition .nutrition-action-panel .food-idea {{
    padding:13px 14px;
}}

@media(min-width:1101px) {{
    .tab-panel.active {{
        display:grid;
        grid-template-columns:repeat(12,minmax(0,1fr));
        gap:12px;
        align-items:start;
    }}
    .tab-panel > .charts-grid,
    #tab-foodprofile > .food-profile {{
        display:contents;
    }}
    .tab-panel > .section-insight {{
        grid-column:1/-1;
        grid-row:1;
        height:auto;
    }}
    .category-ai.section-insight .cat-ai-head {{
        display:flex;
        flex-direction:row;
        align-items:center;
        justify-content:space-between;
        gap:18px;
    }}
    .category-ai.section-insight .cat-ai-actions {{
        width:auto;
        display:flex;
        flex:0 0 auto;
    }}
    .category-ai.section-insight .cat-ai-actions .meta {{
        max-width:260px;
    }}

    #tab-sleep .sleep-metrics {{ grid-column:1/-1; grid-row:2; margin:0; }}
    #tab-sleep .sleep-phases {{ grid-column:1/9; grid-row:3; }}
    #tab-sleep .sleep-latest {{ grid-column:9/-1; grid-row:3; }}
    #tab-sleep .sleep-hr {{ grid-column:1/-1; grid-row:4; }}

    #tab-body .body-weight {{ grid-column:1/9; grid-row:2; }}
    #tab-body .body-measurements {{ grid-column:9/-1; grid-row:2; }}
    #tab-body .body-composition {{ grid-column:1/-1; grid-row:3; }}

    #tab-nutrition .nutrition-diary-card {{
        grid-column:1/9;
        grid-row:2;
    }}
    #tab-nutrition .nutrition-ideas {{
        grid-column:9/-1;
        grid-row:2;
        max-height:640px;
    }}
    #tab-nutrition .nutrition-ideas .food-ideas {{
        max-height:570px;
        overflow:auto;
        padding-right:4px;
        scrollbar-width:thin;
    }}
    #tab-nutrition .nutrition-calories,
    #tab-nutrition .nutrition-calories.is-wide {{
        grid-column:1/8;
        grid-row:3;
    }}
    #tab-nutrition .nutrition-macros {{
        grid-column:8/-1;
        grid-row:3;
    }}
    #tab-nutrition .nutrition-balance {{
        grid-column:1/-1;
        grid-row:4;
    }}

    #tab-foodprofile .food-profile-main {{
        grid-column:1/9;
        grid-row:2;
    }}
    #tab-foodprofile .food-profile-summary {{
        grid-column:9/-1;
        grid-row:2;
    }}

    #tab-activity .activity-overview {{ grid-column:1/-1; grid-row:2; }}
    #tab-activity .activity-workouts {{
        grid-column:1/-1;
        grid-row:3;
        height:auto;
        min-height:382px;
    }}

    #tab-health .health-overview {{ grid-column:1/-1; grid-row:2; }}
    #tab-health .health-stress {{ grid-column:1/7; grid-row:3; }}
    #tab-health .health-spo2 {{ grid-column:7/-1; grid-row:3; }}

    #tab-notes .notes-entry {{ grid-column:1/9; grid-row:1; margin:0; }}
    #tab-notes .notes-context {{
        grid-column:9/-1;
        grid-row:1;
        min-height:262px;
        margin:0;
        display:flex;
        flex-direction:column;
        justify-content:center;
    }}
    #tab-notes .notes-timeline {{ grid-column:1/-1; grid-row:2; }}
}}

@media(max-width:1100px) {{
    .tab-panel.active {{ display:block; }}
    .tab-panel > .charts-grid {{ display:grid; }}
    .category-ai.section-insight {{
        height:auto!important;
        min-height:0;
        margin-bottom:12px;
    }}
    .chart-card,
    .chart-card.nutrition-diary-card {{
        height:auto;
        min-height:350px;
    }}
    .primary-chart,.secondary-chart,.summary-chart {{
        grid-column:1/-1!important;
    }}
    #tab-foodprofile > .food-profile {{ display:grid; }}
}}
@keyframes panel-in {{
    from {{ opacity:0; transform:translateY(6px); }}
    to {{ opacity:1; transform:translateY(0); }}
}}
@media (prefers-reduced-motion:reduce) {{
    *,*:before,*:after {{
        scroll-behavior:auto!important;
        animation-duration:.01ms!important;
        animation-iteration-count:1!important;
        transition-duration:.01ms!important;
    }}
}}
@media(max-width:1280px) {{
    .stats {{ grid-template-columns:repeat(4,minmax(140px,1fr)); }}
}}
@media(max-width:900px) {{
    .app-shell {{ display:block; }}
    .sidebar {{
        position:sticky;
        height:auto;
        top:0;
        padding:8px 12px;
        border-right:0;
        border-bottom:1px solid var(--border);
        flex-direction:row;
        align-items:center;
    }}
    .side-logo {{ width:42px; height:42px; border-radius:13px; margin:0; flex:0 0 auto; }}
    .side-logo .ui-icon {{ width:22px; height:22px; }}
    .sidebar .tabs {{
        min-width:0;
        flex:1;
        flex-direction:row;
        overflow-x:auto;
        scrollbar-width:none;
    }}
    .sidebar .tabs::-webkit-scrollbar {{ display:none; }}
    .sidebar .tab {{
        flex:0 0 auto;
        width:auto;
        min-width:76px;
        min-height:48px;
        padding:6px 10px;
        flex-direction:row;
        font-size:10px;
    }}
    .sidebar .tab.active {{ box-shadow:inset 0 -2px var(--section-accent,#8c7cff); }}
    .sidebar .tab .ui-icon {{ width:18px; height:18px; }}
    .sidebar-foot {{ display:none; }}
    .container {{ padding:14px 14px 50px; }}
}}
@media(max-width:720px) {{
    header {{ grid-template-columns:1fr; align-items:start; }}
    .head-controls {{ justify-content:space-between; }}
    .head-controls .chat-fab {{
        position:static;
        width:38px;
        height:38px;
        border-radius:12px;
        box-shadow:none;
        transform:none;
    }}
    .head-controls .chat-fab:hover {{ transform:none; }}
    .chat-panel {{
        bottom:10px;
        max-height:calc(100vh - 20px);
    }}
    .ai-card {{ grid-template-columns:auto minmax(0,1fr); overflow:hidden; }}
    .ai-card .ai-controls {{
        grid-column:1/-1;
        display:grid;
        grid-template-columns:auto minmax(0,1fr);
        gap:8px;
        width:100%;
        min-width:0;
    }}
    .ai-card .period-switch {{
        grid-column:1/-1;
        width:100%;
        display:flex;
    }}
    .ai-card .period-switch button {{ flex:1; justify-content:center; }}
    .ai-card .ai-controls .btn.primary {{ justify-content:center; }}
    .category-ai .cat-ai-head {{
        align-items:flex-start;
        flex-direction:column;
    }}
    .category-ai .cat-ai-actions {{
        width:100%;
        flex-wrap:wrap;
    }}
    .category-ai .cat-ai-actions .meta {{ margin-right:auto; }}
    .category-ai .cat-ai-body {{ max-height:142px; }}
    .stats {{ grid-template-columns:repeat(2,minmax(0,1fr)); }}
    .charts-grid {{ display:block; }}
    .chart-card {{ margin-bottom:12px; }}
    .chart-wrapper {{ height:270px; }}
    .diary-summary {{ grid-template-columns:1fr; }}
    .diary-overview,
    .macro-bars {{ grid-template-columns:repeat(3, minmax(0, 1fr)); }}
    .sleep-metrics {{ grid-template-columns:repeat(2,minmax(0,1fr)); }}
}}
@media(max-width:430px) {{
    .container {{ padding-left:10px; padding-right:10px; }}
    .brand h1 {{ font-size:21px; }}
    .brand .sub {{ font-size:10.5px; }}
    .period-switch button {{ padding:6px 9px; font-size:10.5px; }}
    .stat {{ min-height:96px; padding:12px; }}
    .stat .value {{ font-size:20px; }}
    .ai-card,.category-ai,.chart-card {{ padding:14px; border-radius:15px; }}
    .data-health-btn {{
        min-width:38px;
        width:38px;
        padding:0;
    }}
    .data-health-btn .data-health-label {{ display:none; }}
    .diary-overview,
    .macro-bars {{ grid-template-columns:1fr; }}
    .sleep-metrics {{ grid-template-columns:1fr; }}
    .sleep-metric {{ min-height:140px; }}
}}
</style>
</head>
<body data-active-section="sleep" data-demo-mode="{demo_mode}">
<svg class="icon-sprite" data-icon-set="tabler-icons" aria-hidden="true">
    <symbol id="icon-pulse" viewBox="0 0 24 24"><path d="M19.5 13.572l-7.5 7.428l-2.896-2.868m-6.117-8.104A5 5 0 0 1 12 7.006a5 5 0 1 1 7.5 6.572"/><path d="M3 13h2l2 3l2-6l1 3h3"/></symbol>
    <symbol id="icon-sparkles" viewBox="0 0 24 24"><path d="M16 18a2 2 0 0 1 2 2a2 2 0 0 1 2-2a2 2 0 0 1-2-2a2 2 0 0 1-2 2m0-12a2 2 0 0 1 2 2a2 2 0 0 1 2-2a2 2 0 0 1-2-2a2 2 0 0 1-2 2M9 18a6 6 0 0 1 6-6a6 6 0 0 1-6-6a6 6 0 0 1-6 6a6 6 0 0 1 6 6"/></symbol>
    <symbol id="icon-sync" viewBox="0 0 24 24"><path d="M20 11A8.1 8.1 0 0 0 4.5 9M4 5v4h4"/><path d="M4 13a8.1 8.1 0 0 0 15.5 2m.5 4v-4h-4"/></symbol>
    <symbol id="icon-sleep" viewBox="0 0 24 24"><path d="M12 3h.393a7.5 7.5 0 0 0 7.92 12.446A9 9 0 1 1 12 2.992V3"/><path d="M17 4a2 2 0 0 0 2 2a2 2 0 0 0-2 2a2 2 0 0 0-2-2a2 2 0 0 0 2-2"/><path d="M19 11h2m-1-1v2"/></symbol>
    <symbol id="icon-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></symbol>
    <symbol id="icon-target" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/><path d="M15 9l5-5M16 4h4v4"/></symbol>
    <symbol id="icon-body" viewBox="0 0 24 24"><path d="M7 20h10M6 6l6-1l6 1M12 3v17"/><path d="m9 12l-3-6l-3 6a3 3 0 0 0 6 0m12 0l-3-6l-3 6a3 3 0 0 0 6 0"/></symbol>
    <symbol id="icon-nutrition" viewBox="0 0 24 24"><path d="M19 3v12h-5c-.023-3.681.184-7.406 5-12m0 12v6h-1v-3M8 4v17M5 4v3a3 3 0 1 0 6 0V4"/></symbol>
    <symbol id="icon-arrow-left" viewBox="0 0 24 24"><path d="M5 12h14M5 12l6-6M5 12l6 6"/></symbol>
    <symbol id="icon-arrow-right" viewBox="0 0 24 24"><path d="M5 12h14M19 12l-6-6M19 12l-6 6"/></symbol>
    <symbol id="icon-sunrise" viewBox="0 0 24 24"><path d="M4 18h16M6 14h12M12 3v4M4.2 8.2l2.8 2.8M19.8 8.2L17 11"/><path d="M8 14a4 4 0 0 1 8 0"/></symbol>
    <symbol id="icon-sun" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6L7 7M17 17l1.4 1.4M18.4 5.6L17 7M7 17l-1.4 1.4"/></symbol>
    <symbol id="icon-apple" viewBox="0 0 24 24"><path d="M12 5c-3-2-7 0-7 5c0 5 3 10 7 10s7-5 7-10c0-5-4-7-7-5"/><path d="M12 5c0-2 1-3 3-4M12 5c2 0 3 0 4 1"/></symbol>
    <symbol id="icon-profile" viewBox="0 0 24 24"><path d="M4 11.319c0 3.102.444 5.319 2.222 7.978c1.351 1.797 3.156 2.247 5.08.988c.426-.268.97-.268 1.397 0c1.923 1.26 3.728.809 5.079-.988C19.556 16.637 20 14.421 20 11.32C20 8.659 18.01 6 15.556 6c-1.267 0-2.41.693-3.22 1.44a.5.5 0 0 1-.672 0C10.855 6.694 9.711 6 8.444 6C5.99 6 4 8.66 4 11.319"/><path d="M7 12c0-1.47.454-2.34 1.5-3M12 7c0-1.2.867-4 3-4"/></symbol>
    <symbol id="icon-activity" viewBox="0 0 24 24"><path d="M12 4a1 1 0 1 0 2 0a1 1 0 1 0-2 0M7 21l3-4m6 4l-2-4l-3-3l1-6M6 12l2-3l4-1l3 3l3 1"/></symbol>
    <symbol id="icon-health" viewBox="0 0 24 24"><path d="M12 21A12 12 0 0 1 3.5 6A12 12 0 0 0 12 3a12 12 0 0 0 8.5 3a12.01 12.01 0 0 1 .378 5"/><path d="m18 22l3.35-3.284a2.143 2.143 0 0 0 .005-3.071a2.242 2.242 0 0 0-3.129-.006l-.224.22l-.223-.22a2.242 2.242 0 0 0-3.128-.006a2.143 2.143 0 0 0-.006 3.071L18 22"/></symbol>
    <symbol id="icon-notes" viewBox="0 0 24 24"><path d="M5 5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V5M9 7h6M9 11h6M9 15h4"/></symbol>
    <symbol id="icon-heart" viewBox="0 0 24 24"><path d="M19.5 13.572l-7.5 7.428l-2.896-2.868m-6.117-8.104A5 5 0 0 1 12 7.006a5 5 0 1 1 7.5 6.572"/><path d="M3 13h2l2 3l2-6l1 3h3"/></symbol>
    <symbol id="icon-weight" viewBox="0 0 24 24"><circle cx="12" cy="6" r="3"/><path d="M6.835 9h10.33a1 1 0 0 1 .984.821l1.637 9A1 1 0 0 1 18.802 20H5.198a1 1 0 0 1-.984-1.179l1.637-9A1 1 0 0 1 6.835 9"/></symbol>
    <symbol id="icon-fire" viewBox="0 0 24 24"><path d="M12 10.941C14.333 7.633 12.167 3.118 11 2c0 3.395-2.235 5.299-3.667 6.706C5.903 10.114 5 12 5 14.294C5 17.998 8.134 21 12 21s7-3.002 7-6.706c0-1.712-1.232-4.403-2.333-5.588c-2.084 3.353-3.257 3.353-4.667 2.235"/></symbol>
    <symbol id="icon-steps" viewBox="0 0 24 24"><path d="M4 6h5.426a1 1 0 0 1 .863.496l1.064 1.823a3 3 0 0 0 1.896 1.407l4.677 1.114A4 4 0 0 1 21 14.73V17a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1"/><path d="m14 13l1-2M8 18v-1a4 4 0 0 0-4-4H3m7-1l1.5-3"/></symbol>
    <symbol id="icon-droplet" viewBox="0 0 24 24"><path d="M7.502 19.423c2.602 2.105 6.395 2.105 8.996 0c2.602-2.105 3.262-5.708 1.566-8.546l-4.89-7.26c-.42-.625-1.287-.803-1.936-.397a1.376 1.376 0 0 0-.41.397l-4.893 7.26c-1.695 2.838-1.035 6.441 1.567 8.546"/></symbol>
    <symbol id="icon-chart-donut" viewBox="0 0 24 24"><path d="M10 3.2A9 9 0 1 0 20.8 14a1 1 0 0 0-1-1H16a4.1 4.1 0 1 1-5-5V4a.9.9 0 0 0-1-.8"/><path d="M15 3.5A9 9 0 0 1 20.5 9H16a9 9 0 0 0-1-1V3.5"/></symbol>
    <symbol id="icon-lungs" viewBox="0 0 24 24"><path d="M6.081 20C7.693 20 9 18.665 9 17.02V7.257C9 6.563 8.448 6 7.768 6c-.205 0-.405.052-.584.15l-.13.083c-1.46 1.059-2.432 2.647-3.404 5.824c-.42 1.37-.636 2.962-.648 4.775c-.012 1.675 1.261 3.054 2.877 3.161l.203.007m11.838 0C16.307 20 15 18.665 15 17.02V7.257C15 6.563 15.552 6 16.233 6c.204 0 .405.052.584.15l.13.083c1.46 1.059 2.432 2.647 3.405 5.824c.42 1.37.636 2.962.648 4.775c.012 1.675-1.261 3.054-2.878 3.161L17.92 20M9 12a3 3 0 0 0 3-3a3 3 0 0 0 3 3M12 4v5"/></symbol>
    <symbol id="icon-ruler" viewBox="0 0 24 24"><path d="M19.875 12c.621 0 1.125.512 1.125 1.143v5.714c0 .631-.504 1.143-1.125 1.143H4a1 1 0 0 1-1-1v-5.857C3 12.512 3.504 12 4.125 12h15.75M9 12v2m-3-2v3m6-3v3m6-3v3m-3-3v2M3 3v4m0-2h18m0-2v4"/></symbol>
    <symbol id="icon-brain" viewBox="0 0 24 24"><path d="M15.5 13a3.5 3.5 0 0 0-3.5 3.5v1a3.5 3.5 0 0 0 7 0v-1.8M8.5 13a3.5 3.5 0 0 1 3.5 3.5v1a3.5 3.5 0 0 1-7 0v-1.8M17.5 16a3.5 3.5 0 0 0 0-7H17M19 9.3V6.5a3.5 3.5 0 0 0-7 0M6.5 16a3.5 3.5 0 0 1 0-7H7M5 9.3V6.5a3.5 3.5 0 0 1 7 0v10"/></symbol>
    <symbol id="icon-message" viewBox="0 0 24 24"><path d="M8 9h8M8 13h6"/><path d="M5 19l-1 3l4-2h9a3 3 0 0 0 3-3V7a3 3 0 0 0-3-3H7a3 3 0 0 0-3 3v9a3 3 0 0 0 1 3"/></symbol>
    <symbol id="icon-trash" viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M9 7l1-3h4l1 3M6 7l1 14h10l1-14"/></symbol>
    <symbol id="icon-close" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></symbol>
    <symbol id="icon-database" viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/></symbol>
</svg>
<div class="app-shell">
<aside class="sidebar" aria-label="Разделы дашборда">
    <div class="side-logo" title="Health Dashboard"><svg class="ui-icon"><use href="#icon-pulse"></use></svg></div>
    <nav class="tabs">
        <button class="tab active" data-tab="sleep" style="--section-accent:#8c7cff" onclick="showTab('sleep', this)"><svg class="ui-icon"><use href="#icon-sleep"></use></svg><span>Сон</span></button>
        <button class="tab" data-tab="body" style="--section-accent:#2dd4d0" onclick="showTab('body', this)"><svg class="ui-icon"><use href="#icon-body"></use></svg><span>Тело</span></button>
        <button class="tab" data-tab="nutrition" style="--section-accent:#ff934d" onclick="showTab('nutrition', this)"><svg class="ui-icon"><use href="#icon-nutrition"></use></svg><span>Питание</span></button>
        <button class="tab" data-tab="foodprofile" style="--section-accent:#45d99a" onclick="showTab('foodprofile', this)"><svg class="ui-icon"><use href="#icon-profile"></use></svg><span>Профиль</span></button>
        <button class="tab" data-tab="activity" style="--section-accent:#3f8cff" onclick="showTab('activity', this)"><svg class="ui-icon"><use href="#icon-activity"></use></svg><span>Активность</span></button>
        <button class="tab" data-tab="health" style="--section-accent:#ff648a" onclick="showTab('health', this)"><svg class="ui-icon"><use href="#icon-health"></use></svg><span>Здоровье</span></button>
        <button class="tab" data-tab="notes" style="--section-accent:#e8ad42" onclick="showTab('notes', this)"><svg class="ui-icon"><use href="#icon-notes"></use></svg><span>Заметки</span></button>
    </nav>
    <div class="sidebar-foot">LOCAL<br>HEALTH HUB</div>
</aside>
<main class="dashboard-main">
<div class="container">

<header class="topbar">
    <div class="brand">
        <div class="brand-kicker">Personal health intelligence</div>
        <h1>Health Dashboard</h1>
        <div class="sub">{display_name}, {age} · {height}см · {weight}кг → цель {weight_goal}кг · пост {fasting_plan}</div>
    </div>
    <div class="head-controls">
        <div class="sync-info">
            Синхронизация <span class="sync-dot"></span><br>
            <span id="syncTime">{synced_at}</span>
        </div>
        <button class="btn sm data-health-btn" id="dataHealthBtn" type="button" data-health-state="unknown" onclick="toggleDataHealth()" aria-label="Состояние источников данных">
            <span class="data-health-dot"></span><span class="data-health-label" id="dataHealthLabel">Данные</span>
        </button>
        <button class="btn icon sm" onclick="doSync()" title="Обновить данные с телефона" aria-label="Обновить данные"><svg class="ui-icon"><use href="#icon-sync"></use></svg></button>
        <button class="chat-fab" id="chatFab" type="button" onclick="toggleChat()" title="Спросить AI" aria-label="Открыть AI-ассистента"><svg class="ui-icon"><use href="#icon-message"></use></svg></button>
    </div>
    <div class="sync-notice {sync_notice_class}">{sync_notice_text}</div>
</header>
<div class="demo-banner" {demo_banner_hidden}><svg class="ui-icon"><use href="#icon-database"></use></svg>Синтетические данные · безопасный публичный демо-режим</div>

<!-- Compact AI card -->
<div class="ai-card" id="aiCard">
    <div class="ai-icon"><svg class="ui-icon"><use href="#icon-sparkles"></use></svg></div>
    <div class="ai-body">
        <div class="ai-summary-line" id="aiSummary">…</div>
        <div class="ai-meta" id="aiMeta">Gemini</div>
    </div>
    <div class="ai-controls">
        <div class="period-switch">
            <button data-period="day" onclick="switchPeriod('day')">День</button>
            <button data-period="week" class="active" onclick="switchPeriod('week')">Неделя</button>
            <button data-period="month" onclick="switchPeriod('month')">Месяц</button>
        </div>
        <button class="btn icon sm" onclick="refreshAI()" title="Обновить анализ" aria-label="Обновить AI-анализ" id="refreshAiBtn"><svg class="ui-icon"><use href="#icon-sync"></use></svg></button>
        <button class="btn sm primary" onclick="openAiModal()">Подробнее →</button>
    </div>
</div>

<div class="stats" id="sectionStats" aria-live="polite" aria-label="Ключевые показатели раздела"></div>

<div class="tab-panel active" id="tab-sleep">
    <div class="category-ai section-insight" id="catAi-sleep" data-category="sleep">
        <div class="cat-ai-head">
            <div class="cat-ai-title"><span class="icon"><svg class="ui-icon"><use href="#icon-sleep"></use></svg></span>AI-разбор сна</div>
            <div class="cat-ai-actions">
                <span class="meta" id="catAiMeta-sleep"></span>
                <button class="btn icon sm" onclick="refreshCategory('sleep')" title="Обновить разбор"><svg class="ui-icon"><use href="#icon-sync"></use></svg></button>
                <button class="btn sm" onclick="openCategoryModal('sleep')">Подробнее →</button>
            </div>
        </div>
        <div class="cat-ai-body" id="catAiBody-sleep">Загружаю…</div>
    </div>
    <section class="sleep-metrics" aria-label="Ключевые показатели сна">
        <article class="sleep-metric" data-sleep-metric="sleep-debt" style="--sleep-color:#8c7cff">
            <div class="sleep-metric-head">
                <div class="sleep-metric-title"><svg class="ui-icon"><use href="#icon-sleep"></use></svg>Расчётный долг</div>
                <span class="sleep-confidence {sleep_confidence}">{sleep_confidence_label}</span>
            </div>
            <div class="sleep-metric-value">{sleep_debt_label}</div>
            <div class="sleep-debt-bars" aria-label="Накопление долга по записанным ночам">{sleep_debt_bars}</div>
            <div class="sleep-metric-note">Ориентир <strong>{sleep_target_label}</strong> · {sleep_recent_nights} записанных ночей</div>
        </article>
        <article class="sleep-metric" data-sleep-metric="sleep-regularity" style="--sleep-color:#a578ff">
            <div class="sleep-metric-head">
                <div class="sleep-metric-title"><svg class="ui-icon"><use href="#icon-pulse"></use></svg>Регулярность</div>
                <span>{sleep_regularity_label}</span>
            </div>
            <div class="sleep-metric-value">{sleep_regularity_value}<span class="unit">{sleep_regularity_unit}</span></div>
            <div class="sleep-metric-note">Засыпание ±{sleep_bed_spread}; подъём ±{sleep_wake_spread}. Чем меньше разброс, тем устойчивее режим.</div>
        </article>
        <article class="sleep-metric" data-sleep-metric="sleep-window" style="--sleep-color:#5d9dff">
            <div class="sleep-metric-head">
                <div class="sleep-metric-title"><svg class="ui-icon"><use href="#icon-clock"></use></svg>Типичное окно</div>
                <span>последние ночи</span>
            </div>
            <div class="sleep-window"><span>{sleep_typical_bedtime}</span><span class="line"></span><span>{sleep_typical_waketime}</span></div>
            <div class="sleep-metric-note">Средний фактический сон <strong>{sleep_average_label}</strong>, без времени пробуждений.</div>
        </article>
        <article class="sleep-metric" data-sleep-metric="sleep-action" style="--sleep-color:#f5c36b">
            <div class="sleep-metric-head">
                <div class="sleep-metric-title"><svg class="ui-icon"><use href="#icon-target"></use></svg>Следующая ночь</div>
                <span>мягкое восстановление</span>
            </div>
            <div class="sleep-metric-value">{sleep_recommended_label}</div>
            <div class="sleep-metric-note">Добавлено не более часа к ориентиру: долг лучше снижать постепенно, <strong>не закрывать за одну ночь</strong>.</div>
        </article>
    </section>
    <div class="charts-grid">
        <div class="chart-card sleep-phases primary-chart">
            <h2><span class="icon"><svg class="ui-icon"><use href="#icon-sleep"></use></svg></span>Фазы сна <span class="hint">мин / ночь</span></h2>
            <div class="chart-wrapper"><canvas id="sleepChart"></canvas></div>
        </div>
        <div class="chart-card sleep-hr secondary-chart">
            <h2><span class="icon"><svg class="ui-icon"><use href="#icon-heart"></use></svg></span>ЧСС во сне</h2>
            <div class="chart-wrapper"><canvas id="sleepHrChart"></canvas></div>
        </div>
        <div class="chart-card sleep-latest summary-chart">
            <h2><span class="icon"><svg class="ui-icon"><use href="#icon-chart-donut"></use></svg></span>Последняя ночь</h2>
            <div class="chart-wrapper"><canvas id="stagesChart"></canvas></div>
        </div>
    </div>
</div>

<div class="tab-panel" id="tab-body">
    <div class="category-ai section-insight" id="catAi-body" data-category="body">
        <div class="cat-ai-head">
            <div class="cat-ai-title"><span class="icon"><svg class="ui-icon"><use href="#icon-body"></use></svg></span>AI-разбор состава тела</div>
            <div class="cat-ai-actions">
                <span class="meta" id="catAiMeta-body"></span>
                <button class="btn icon sm" onclick="refreshCategory('body')" title="Обновить разбор"><svg class="ui-icon"><use href="#icon-sync"></use></svg></button>
                <button class="btn sm" onclick="openCategoryModal('body')">Подробнее →</button>
            </div>
        </div>
        <div class="cat-ai-body" id="catAiBody-body">Загружаю…</div>
    </div>
    <div class="charts-grid">
        <div class="chart-card body-weight primary-chart">
            <h2><span class="icon"><svg class="ui-icon"><use href="#icon-weight"></use></svg></span>Вес <span class="hint">90 дней</span></h2>
            <div class="chart-wrapper"><canvas id="weightChart"></canvas></div>
        </div>
        <div class="chart-card body-composition secondary-chart">
            <h2><span class="icon"><svg class="ui-icon"><use href="#icon-body"></use></svg></span>Состав тела</h2>
            <div class="chart-wrapper"><canvas id="bodyCompChart"></canvas></div>
        </div>
    </div>

    <div class="chart-card body-measurements summary-chart">
        <h2><span class="icon"><svg class="ui-icon"><use href="#icon-ruler"></use></svg></span>Замеры тела <span class="hint">сантиметры, сравнение со временем</span></h2>
        <div id="measurementsLatest" style="margin-bottom:14px;">Загружаю…</div>
        <details style="margin-bottom:14px;">
            <summary style="cursor:pointer;color:var(--accent2);font-size:13px;padding:6px 0;">+ Добавить / обновить замер</summary>
            <div class="measurements-form" style="margin-top:10px;display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;">
                <input type="date" id="msrDate" style="padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;font-size:13px;">
                <input type="number" step="0.1" id="msr_chest_cm" placeholder="Грудь объём" style="padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;font-size:13px;">
                <input type="number" step="0.1" id="msr_shoulders_cm" placeholder="Плечи ширина" style="padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;font-size:13px;">
                <input type="number" step="0.1" id="msr_waist_cm" placeholder="Талия объём" style="padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;font-size:13px;">
                <input type="number" step="0.1" id="msr_hips_cm" placeholder="Бёдра объём" style="padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;font-size:13px;">
                <input type="number" step="0.1" id="msr_biceps_cm" placeholder="Бицепс объём" style="padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;font-size:13px;">
                <input type="number" step="0.1" id="msr_thigh_cm" placeholder="Бедро объём" style="padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;font-size:13px;">
                <input type="number" step="0.1" id="msr_calf_cm" placeholder="Икра объём" style="padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;font-size:13px;">
                <input type="number" step="0.1" id="msr_neck_cm" placeholder="Шея объём" style="padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;font-size:13px;">
                <input type="number" step="0.1" id="msr_arm_length_cm" placeholder="Рука длина" style="padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;font-size:13px;">
                <input type="number" step="0.1" id="msr_foot_cm" placeholder="Стопа длина" style="padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;font-size:13px;">
                <button class="btn primary sm" onclick="saveMeasurements()" style="grid-column:span 2;">Сохранить</button>
            </div>
        </details>
        <div id="measurementsHistory"></div>
    </div>
</div>

<div class="tab-panel" id="tab-nutrition">
    <div class="category-ai section-insight" id="catAi-nutrition" data-category="nutrition">
        <div class="cat-ai-head">
            <div class="cat-ai-title"><span class="icon"><svg class="ui-icon"><use href="#icon-nutrition"></use></svg></span>AI-разбор питания</div>
            <div class="cat-ai-actions">
                <span class="meta" id="catAiMeta-nutrition"></span>
                <button class="btn icon sm" onclick="refreshCategory('nutrition')" title="Обновить разбор"><svg class="ui-icon"><use href="#icon-sync"></use></svg></button>
                <button class="btn sm" onclick="openCategoryModal('nutrition')">Подробнее →</button>
            </div>
        </div>
        <div class="cat-ai-body" id="catAiBody-nutrition">Загружаю…</div>
    </div>
    <div class="chart-card nutrition-diary-card primary-chart">
        <h2><span class="icon"><svg class="ui-icon"><use href="#icon-nutrition"></use></svg></span>Дневник питания <span class="hint" id="nutritionHeaderHint">{last_nutr_date}</span></h2>
        <div class="nutrition-diary">
            <div class="diary-nav">
                <button type="button" id="nutritionPrevDay" onclick="shiftNutritionDay(1)" title="День раньше" aria-label="День раньше"><svg class="ui-icon"><use href="#icon-arrow-left"></use></svg></button>
                <div class="date-box">
                    <div class="date-main" id="nutritionSelectedDate">{last_nutr_date}</div>
                    <div class="date-sub" id="nutritionSelectedMeta">еда, бюджет и баланс за выбранный день</div>
                </div>
                <button type="button" id="nutritionNextDay" onclick="shiftNutritionDay(-1)" title="День позже" aria-label="День позже"><svg class="ui-icon"><use href="#icon-arrow-right"></use></svg></button>
            </div>
            <div class="nutrition-days" id="nutritionDays" aria-label="Последние дни питания"></div>
            <div id="nutritionTodaySummary"></div>
            <div class="meals-grid" id="mealsGrid"></div>
        </div>
    </div>
    <div class="charts-grid">
        <div class="chart-card nutrition-balance secondary-chart">
            <h2><span class="icon"><svg class="ui-icon"><use href="#icon-weight"></use></svg></span>Баланс по дням <span class="hint">{energy_note}</span></h2>
            <div class="chart-wrapper"><canvas id="energyBalanceChart"></canvas></div>
        </div>
        <div class="chart-card nutrition-calories secondary-chart">
            <h2><span class="icon"><svg class="ui-icon"><use href="#icon-fire"></use></svg></span>Калории <span class="hint" id="kcalChartHint">зелёный — в цели, красный — выше</span></h2>
            <div class="chart-wrapper"><canvas id="kcalChart"></canvas></div>
        </div>
        <div class="chart-card nutrition-macros secondary-chart">
            <h2><span class="icon"><svg class="ui-icon"><use href="#icon-nutrition"></use></svg></span>БЖУ</h2>
            <div class="chart-wrapper"><canvas id="macrosChart"></canvas></div>
        </div>
    </div>
    <div class="chart-card nutrition-ideas nutrition-action-panel summary-chart">
        <h2><span class="icon"><svg class="ui-icon"><use href="#icon-nutrition"></use></svg></span>Что приготовить в ближайшие дни <span class="hint">не случайные снеки, а полезные группы еды</span></h2>
        <div class="food-ideas" id="foodIdeas"></div>
    </div>
</div>

<div class="tab-panel" id="tab-foodprofile">
    <div class="category-ai section-insight" id="catAi-foodprofile" data-category="foodprofile">
        <div class="cat-ai-head">
            <div class="cat-ai-title"><span class="icon"><svg class="ui-icon"><use href="#icon-profile"></use></svg></span>AI-разбор пищевого профиля</div>
            <div class="cat-ai-actions">
                <span class="meta" id="catAiMeta-foodprofile"></span>
                <button class="btn icon sm" onclick="refreshCategory('foodprofile')" title="Обновить разбор"><svg class="ui-icon"><use href="#icon-sync"></use></svg></button>
                <button class="btn sm" onclick="openCategoryModal('foodprofile')">Подробнее →</button>
            </div>
        </div>
        <div class="cat-ai-body" id="catAiBody-foodprofile">Загружаю…</div>
    </div>
    <div class="food-profile">
        <div class="profile-card food-profile-main">
            <h2>Глубокий пищевой тест</h2>
            <div class="hint">Это не ежедневная задача. Заполнил один раз — и советы по еде перестают предлагать продукты, которые тебе не заходят. Склад продуктов дома можно не вести.</div>
            <h2>Не предлагать</h2>
            <div class="profile-grid">
                <label class="chip-check"><input type="checkbox" data-avoid="legumes">Фасоль / чечевица / нут</label>
                <label class="chip-check"><input type="checkbox" data-avoid="fish">Рыба</label>
                <label class="chip-check"><input type="checkbox" data-avoid="dairy">Творог / йогурт</label>
                <label class="chip-check"><input type="checkbox" data-avoid="eggs">Яйца</label>
                <label class="chip-check"><input type="checkbox" data-avoid="vegetables">Овощи</label>
            </div>
            <h2 style="margin-top:14px;">Что обычно норм</h2>
            <div class="profile-grid">
                <label class="chip-check"><input type="checkbox" data-prefer="курица">Курица</label>
                <label class="chip-check"><input type="checkbox" data-prefer="яйца">Яйца</label>
                <label class="chip-check"><input type="checkbox" data-prefer="творог">Творог</label>
                <label class="chip-check"><input type="checkbox" data-prefer="сыр">Сыр</label>
                <label class="chip-check"><input type="checkbox" data-prefer="рис">Рис</label>
                <label class="chip-check"><input type="checkbox" data-prefer="гречка">Гречка</label>
                <label class="chip-check"><input type="checkbox" data-prefer="картофель">Картофель</label>
                <label class="chip-check"><input type="checkbox" data-prefer="макароны">Макароны</label>
            </div>
            <h2 style="margin-top:14px;">Свободно</h2>
            <textarea id="foodProfileNotes" placeholder="Например: не люблю супы, рыбу редко, фасоль не ем, готовить максимум 20 минут, духовку лень, хочу простые блюда..."></textarea>
            <div class="profile-actions">
                <label class="chip-check"><input type="checkbox" id="recipeMode">Показывать рецепты</label>
                <button class="btn primary sm" id="foodProfileSaveBtn" onclick="saveFoodProfile()">Сохранить профиль</button>
            </div>
        </div>
        <div class="profile-card food-profile-summary">
            <h2>Как это будет работать</h2>
            <div class="profile-kv">
                <div><strong>Без давления.</strong> Не будет недельных целей и обязательных чек-листов.</div>
                <div><strong>Советы по еде.</strong> Блок “Что приготовить” фильтрует запрещённые группы и предлагает полноценные блюда.</div>
                <div><strong>AI-анализ.</strong> При следующей генерации Gemini будет видеть пищевой профиль и не будет советовать то, что ты отметил как “не ем”.</div>
                <div><strong>Статус:</strong> <span id="foodProfileStatus">загружаю…</span></div>
                <div><strong>Глубокий тест:</strong> <span id="foodProfileDeepStatus">загружаю…</span></div>
                <div><strong>Не предлагать:</strong> <span id="foodProfileAvoidPreview">загружаю…</span></div>
                <div><strong>Обычно заходит:</strong> <span id="foodProfileLikePreview">загружаю…</span></div>
            </div>
        </div>
    </div>
</div>

<div class="tab-panel" id="tab-activity">
    <div class="category-ai section-insight" id="catAi-activity" data-category="activity">
        <div class="cat-ai-head">
            <div class="cat-ai-title"><span class="icon"><svg class="ui-icon"><use href="#icon-activity"></use></svg></span>AI-разбор + упражнения на дома</div>
            <div class="cat-ai-actions">
                <span class="meta" id="catAiMeta-activity"></span>
                <button class="btn icon sm" onclick="refreshCategory('activity')" title="Обновить разбор"><svg class="ui-icon"><use href="#icon-sync"></use></svg></button>
                <button class="btn sm" onclick="openCategoryModal('activity')">Подробнее →</button>
            </div>
        </div>
        <div class="cat-ai-body" id="catAiBody-activity">Загружаю…</div>
    </div>
    <div class="charts-grid">
        <div class="chart-card activity-overview primary-chart">
            <h2><span class="icon"><svg class="ui-icon"><use href="#icon-activity"></use></svg></span>Шаги, активные минуты и Google Fit</h2>
            <div class="chart-wrapper"><canvas id="stepsChart"></canvas></div>
        </div>
        <div class="chart-card activity-workouts secondary-chart">
            <h2><span class="icon"><svg class="ui-icon"><use href="#icon-activity"></use></svg></span>Тренировки <span class="hint">KS Fit / Mobvoi / Google Fit через Health Connect</span></h2>
            <div class="workout-list" id="workoutList"></div>
        </div>
    </div>
</div>

<div class="tab-panel" id="tab-health">
    <div class="category-ai section-insight" id="catAi-health" data-category="health">
        <div class="cat-ai-head">
            <div class="cat-ai-title"><span class="icon"><svg class="ui-icon"><use href="#icon-health"></use></svg></span>AI-отчёт по сердцу и стрессу</div>
            <div class="cat-ai-actions">
                <span class="meta" id="catAiMeta-health"></span>
                <button class="btn icon sm" onclick="refreshCategory('health')" title="Обновить разбор"><svg class="ui-icon"><use href="#icon-sync"></use></svg></button>
                <button class="btn sm" onclick="openCategoryModal('health')">Подробнее →</button>
            </div>
        </div>
        <div class="cat-ai-body" id="catAiBody-health">Загружаю…</div>
    </div>
    <div class="charts-grid">
        <div class="chart-card health-overview primary-chart">
            <h2><span class="icon"><svg class="ui-icon"><use href="#icon-heart"></use></svg></span>ЧСС (дневной диапазон)</h2>
            <div class="chart-wrapper"><canvas id="dailyHrChart"></canvas></div>
        </div>
        <div class="chart-card health-stress secondary-chart">
            <h2><span class="icon"><svg class="ui-icon"><use href="#icon-brain"></use></svg></span>Стресс</h2>
            <div class="chart-wrapper"><canvas id="stressChart"></canvas></div>
        </div>
        <div class="chart-card health-spo2 secondary-chart">
            <h2><span class="icon"><svg class="ui-icon"><use href="#icon-lungs"></use></svg></span>SpO₂</h2>
            <div class="chart-wrapper"><canvas id="spo2Chart"></canvas></div>
        </div>
    </div>
</div>

<div class="tab-panel" id="tab-notes">
    <div class="notes-status notes-context">
        <div class="label">Контекст для Gemini</div>
        <div class="msg">{notes_status_message}</div>
        <div class="meta">{notes_status_meta}</div>
    </div>
    <div class="notes-composer notes-entry" id="composerCard" style="display:{composer_display};">
        <h3><svg class="ui-icon"><use href="#icon-notes"></use></svg> Добавить заметку с ПК</h3>
        <textarea id="noteText" placeholder="Самочувствие, симптомы, тренировка, мысли — любой длины, без лимита YAZIO"></textarea>
        <div class="row">
            <input type="date" id="noteDate" value="{today}">
            <input type="text" id="noteTags" placeholder="теги через запятую (sick, energy, pain)">
            <button class="btn primary sm" onclick="submitNote(true)">Сохранить</button>
            <button class="btn sm" onclick="submitNote(false)">＋ Добавить к дню</button>
        </div>
        <div style="margin-top:12px;padding-top:12px;border-top:1px solid var(--border);">
            <button class="symptom-link" onclick="openSymptomModal()" title="Что-то прихватило сейчас? Отдельный AI-разбор по острому симптому">
                <span class="ico">🔍</span>Разобрать острый симптом — топ-3 причины по твоим данным
            </button>
        </div>
    </div>
    <div class="notes-composer notes-entry" id="composerOfflineHint" style="display:{offline_display};">
        <h3 style="margin:0 0 6px 0;color:var(--dim);">💡 Быстрое редактирование заметок</h3>
        <div style="color:var(--muted);font-size:12.5px;line-height:1.6;">
            Для редактирования из браузера открой
            <a href="http://127.0.0.1:8787/" style="color:var(--accent);text-decoration:none;">интерактивную версию</a>.
            Если она не открывается — запусти
            <code style="color:var(--warn);background:rgba(0,0,0,0.3);padding:2px 6px;border-radius:4px;">python dashboard_server.py</code>.
            Или из терминала:
            <code style="color:var(--accent);background:rgba(0,0,0,0.3);padding:2px 6px;border-radius:4px;">python note.py</code>.
            Если локальный сервер уже запущен, эта file:// страница сама включит форму выше.
        </div>
    </div>
    <div class="notes-timeline" id="notesSection"></div>
</div>

<footer>
    Mobvoi Health · Zepp Life · YAZIO · Gemini 3.1 Pro Preview с откатом на 2.5 · health_sync.py → build_dashboard.py
</footer>

</div>
</main>
</div>

<!-- Data Health Modal -->
<div class="modal-backdrop" id="dataHealthModal" onclick="if(event.target===this) closeDataHealth()">
    <div class="modal" style="max-width:620px;">
        <button class="modal-close" onclick="closeDataHealth()" aria-label="Закрыть"><svg class="ui-icon"><use href="#icon-close"></use></svg></button>
        <h2><svg class="ui-icon"><use href="#icon-database"></use></svg>Состояние данных</h2>
        <div class="modal-meta" id="dataHealthSummary">Проверяю источники…</div>
        <div class="data-health-grid" id="dataHealthSources"></div>
        <div class="data-health-jobs" id="dataHealthJobs"></div>
    </div>
</div>

<!-- AI Modal -->
<div class="modal-backdrop" id="aiModal" onclick="if(event.target===this) closeAiModal()">
    <div class="modal">
        <button class="modal-close" onclick="closeAiModal()">×</button>
        <h2><span>🤖</span>AI-анализ <span id="modalPeriodLabel" style="color:var(--accent);font-weight:500;font-size:15px;"></span></h2>
        <div class="modal-meta" id="modalMeta"></div>
        <div style="display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap;">
            <div class="period-switch">
                <button data-period="day" onclick="switchPeriod('day')">День</button>
                <button data-period="week" class="active" onclick="switchPeriod('week')">Неделя</button>
                <button data-period="month" onclick="switchPeriod('month')">Месяц</button>
            </div>
            <div class="period-switch">
                <button data-period="sleep" onclick="switchPeriod('sleep')"><svg class="ui-icon"><use href="#icon-sleep"></use></svg>Сон</button>
                <button data-period="body" onclick="switchPeriod('body')"><svg class="ui-icon"><use href="#icon-body"></use></svg>Тело</button>
                <button data-period="nutrition" onclick="switchPeriod('nutrition')"><svg class="ui-icon"><use href="#icon-nutrition"></use></svg>Питание</button>
                <button data-period="foodprofile" onclick="switchPeriod('foodprofile')"><svg class="ui-icon"><use href="#icon-profile"></use></svg>Профиль еды</button>
                <button data-period="activity" onclick="switchPeriod('activity')"><svg class="ui-icon"><use href="#icon-activity"></use></svg>Движение</button>
                <button data-period="health" onclick="switchPeriod('health')"><svg class="ui-icon"><use href="#icon-health"></use></svg>Сердце</button>
            </div>
        </div>
        <div class="ai-content" id="aiFullContent">Загрузка…</div>
        <div style="margin-top:20px;display:flex;gap:8px;">
            <button class="btn sm" onclick="refreshAI()" id="modalRefreshBtn">⟳ Обновить анализ</button>
        </div>
    </div>
</div>

<!-- Symptom Acute Modal -->
<div class="modal-backdrop" id="symptomModal" onclick="if(event.target===this) closeSymptomModal()">
    <div class="modal">
        <button class="modal-close" onclick="closeSymptomModal()">×</button>
        <h2><span>🚨</span>Острый разбор симптома</h2>
        <div class="modal-meta">Топ-3 вероятные причины на основе данных за 7 дней + похожих эпизодов в прошлом. Это <strong>не диагноз</strong> — pattern matching по твоим же данным.</div>

        <input type="text" class="symptom-input" id="symptomInput"
               placeholder="Опиши что чувствуешь: «давит в груди», «болит живот после обеда»…"
               onkeydown="if(event.key==='Enter') askSymptom()" />

        <div class="symptom-quick">
            <button class="btn sm" onclick="quickSymptom('болит голова, мигрень')">🤕 Голова</button>
            <button class="btn sm" onclick="quickSymptom('сердце прихватило, давит в груди')">💔 Сердце</button>
            <button class="btn sm" onclick="quickSymptom('болит живот, спазм желудка')">🤢 Живот</button>
            <button class="btn sm" onclick="quickSymptom('тревога, паническое состояние, руминация')">😰 Тревога</button>
            <button class="btn sm" onclick="quickSymptom('сильная усталость, нет сил, разбитость')">😩 Усталость</button>
            <button class="btn sm" onclick="quickSymptom('одышка, тяжело дышать')">🫁 Дыхание</button>
            <button class="btn go" onclick="askSymptom()">→ Разобрать</button>
        </div>

        <div class="ai-content" id="symptomResult">
            <em>Опиши симптом текстом или нажми быструю кнопку и → Разобрать.</em>
        </div>
        <div class="symptom-meta" id="symptomMeta"></div>
    </div>
</div>

<!-- Workout Share Modal -->
<div class="modal-backdrop" id="workoutShareModal" onclick="if(event.target===this) closeWorkoutShareModal()">
    <div class="modal workout-share-modal">
        <button class="modal-close" onclick="closeWorkoutShareModal()">×</button>
        <h2>Карта тренировки</h2>
        <div class="modal-meta" id="shareMeta">Карточка показывается только для тренировок с реальным GPS-маршрутом.</div>
        <div class="share-card" id="workoutShareCard">
            <div class="share-map">
                <canvas id="shareMapCanvas"></canvas>
                <div class="share-brand">Health Dashboard</div>
                <div class="share-map-badge" id="shareMapBadge">GPS-маршрут</div>
            </div>
            <div class="share-card-body">
                <div class="share-title" id="shareTitle">Тренировка</div>
                <div class="share-time" id="shareTime">—</div>
                <div class="share-stats">
                    <div class="share-stat"><span class="num" id="shareDistance">—</span><span class="lbl" id="shareDistanceLabel">км</span></div>
                    <div class="share-stat"><span class="num" id="shareDuration">—</span><span class="lbl" id="shareDurationLabel">мин</span></div>
                    <div class="share-stat"><span class="num" id="shareKcal">—</span><span class="lbl" id="shareKcalLabel">ккал</span></div>
                </div>
            </div>
        </div>
        <div class="share-actions">
            <div class="share-note" id="shareNote">Рисую маршрут только из реальных GPS-точек тренировки.</div>
            <button class="btn sm" onclick="copyShareText()">Скопировать текст</button>
        </div>
    </div>
</div>

<div class="toast" id="toast"></div>

<script>
const DATA = {chart_json};
const SLEEP_METRICS = {sleep_metrics_json};
const SECTION_KPIS = {section_kpis_json};
const MEALS = {meals_json};
const NUTRITION_DIARY = {nutrition_diary_json};
const WORKOUTS = {workouts_json};
const FOOD_IDEAS = {food_ideas_json};
const FOOD_PROFILE = {food_profile_json};
const MEASUREMENTS = {measurements_json};
const MEASUREMENT_FIELDS = {measurement_fields_json};
const FEELINGS = {feelings_json};
const AI_CACHE = {ai_cache_json};
const SERVER_MODE = {server_mode};
const DEMO_MODE = {demo_mode};
const API_BASE = SERVER_MODE ? '' : 'http://127.0.0.1:8787';
let BRIDGE_ONLINE = SERVER_MODE;
function apiUrl(path) {{
    return (SERVER_MODE ? '' : API_BASE) + path;
}}

let currentPeriod = 'week';

// Chart defaults
Chart.defaults.color = '#93a2b9';
Chart.defaults.borderColor = 'rgba(151,174,211,0.09)';
Chart.defaults.font.family = "'Manrope','Segoe UI',sans-serif";
Chart.defaults.font.size = 11;

// --- UI helpers ---
function escapeHtml(s) {{
    if (s == null) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}
function formatAge(iso) {{
    if (!iso) return '—';
    try {{
        const then = new Date(iso);
        const sec = Math.floor((Date.now() - then.getTime()) / 1000);
        if (sec < 60) return sec + 'с назад';
        if (sec < 3600) return Math.floor(sec/60) + 'м назад';
        if (sec < 86400) return Math.floor(sec/3600) + 'ч назад';
        return Math.floor(sec/86400) + 'д назад';
    }} catch (e) {{ return iso; }}
}}
function toast(msg, kind) {{
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.className = 'toast show ' + (kind || '');
    clearTimeout(t._timer);
    t._timer = setTimeout(() => t.classList.remove('show'), 3500);
}}

function renderSectionStats(name) {{
    const host = document.getElementById('sectionStats');
    if (!host) return;
    const cards = SECTION_KPIS[name] || SECTION_KPIS.sleep || [];
    host.dataset.section = name;
    host.innerHTML = cards.map(item => {{
        const icon = /^[a-z0-9-]+$/.test(item.icon || '') ? item.icon : 'pulse';
        const tone = ['ok', 'warn', 'bad'].includes(item.tone) ? item.tone : '';
        const color = /^#[0-9a-f]{{6}}$/i.test(item.color || '') ? item.color : '#8c7cff';
        return `
            <article class="stat" style="--stat-color:${{color}}">
                <div class="stat-head">
                    <svg class="ui-icon stat-icon"><use href="#icon-${{icon}}"></use></svg>
                    <div class="label">${{escapeHtml(item.label)}}</div>
                </div>
                <div class="value">${{escapeHtml(item.value)}}${{item.unit ? `<span class="u">${{escapeHtml(item.unit)}}</span>` : ''}}</div>
                <div class="delta ${{tone}}">${{escapeHtml(item.delta)}}</div>
            </article>
        `;
    }}).join('');
}}

function showTab(name, btn) {{
    document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    const panel = document.getElementById('tab-' + name);
    if (!panel) return;
    panel.classList.add('active');
    renderSectionStats(name);
    document.body.dataset.activeSection = name;
    try {{ localStorage.setItem('health-dashboard-tab', name); }} catch (e) {{}}
    if (window.location.hash !== '#' + name) {{
        history.replaceState(null, '', '#' + name);
    }}
    window.dispatchEvent(new Event('resize'));
}}
window.addEventListener('hashchange', () => {{
    const linkedTab = window.location.hash.slice(1);
    if (document.getElementById('tab-' + linkedTab) && document.body.dataset.activeSection !== linkedTab) {{
        showTab(linkedTab);
    }}
}});

// --- AI ---
const PERIOD_LABEL = {{
    day: 'День', week: 'Неделя', month: 'Месяц',
    sleep: 'Сон', body: 'Тело', nutrition: 'Питание',
    foodprofile: 'Профиль еды',
    activity: 'Активность', health: 'Здоровье',
}};
const GLOBAL_PERIODS = ['day', 'week', 'month'];
const CATEGORY_PERIODS = ['sleep', 'body', 'nutrition', 'foodprofile', 'activity', 'health'];

function extractSummary(html) {{
    const m = html.match(/<div class="ai-summary">([\s\S]*?)<\/div>/);
    if (m) return m[1].replace(/<[^>]+>/g, '').trim();
    // fallback: первый <p>
    const p = html.match(/<p>([\s\S]*?)<\/p>/);
    if (p) return p[1].replace(/<[^>]+>/g, '').trim().substring(0, 200);
    return html.replace(/<[^>]+>/g, '').trim().substring(0, 200);
}}

const AI_ALLOWED_TAGS = new Set(['DIV', 'H3', 'P', 'UL', 'OL', 'LI', 'STRONG', 'EM', 'BR']);
const AI_BLOCKED_TAGS = new Set(['SCRIPT', 'STYLE', 'IFRAME', 'OBJECT', 'EMBED', 'SVG', 'MATH', 'LINK', 'META']);

function sanitizeAiHtml(html) {{
    const source = document.createElement('template');
    const target = document.createElement('template');
    source.innerHTML = String(html || '');

    function appendSafe(node, parent) {{
        if (node.nodeType === Node.TEXT_NODE) {{
            parent.appendChild(document.createTextNode(node.textContent || ''));
            return;
        }}
        if (node.nodeType !== Node.ELEMENT_NODE) return;
        if (AI_BLOCKED_TAGS.has(node.tagName)) return;
        if (!AI_ALLOWED_TAGS.has(node.tagName)) {{
            Array.from(node.childNodes).forEach(child => appendSafe(child, parent));
            return;
        }}

        const clean = document.createElement(node.tagName.toLowerCase());
        if (node.tagName === 'DIV' && node.classList.contains('ai-summary')) {{
            clean.className = 'ai-summary';
        }}
        parent.appendChild(clean);
        Array.from(node.childNodes).forEach(child => appendSafe(child, clean));
    }}

    Array.from(source.content.childNodes).forEach(node => appendSafe(node, target.content));
    return target.innerHTML;
}}

function renderAI(period) {{
    const cache = AI_CACHE[period];
    // Верхний card — только для глобальных периодов (day/week/month).
    if (GLOBAL_PERIODS.includes(period)) {{
        const sumEl = document.getElementById('aiSummary');
        const metaEl = document.getElementById('aiMeta');
        if (!cache || !cache.text) {{
            sumEl.textContent = 'Анализ ещё не создан. Нажми ⟳ для генерации.';
            metaEl.textContent = 'Gemini · ' + period;
        }} else {{
            sumEl.textContent = extractSummary(cache.text);
            metaEl.textContent = `${{cache.model || 'Gemini'}} · ${{formatAge(cache.iso)}}`;
        }}
    }}
    // Модал — показывает любой период
    const modalContent = document.getElementById('aiFullContent');
    const modalMeta = document.getElementById('modalMeta');
    if (!cache || !cache.text) {{
        modalContent.innerHTML = '<em>Нет данных. Нажми «Обновить анализ».</em>';
        modalMeta.textContent = '';
    }} else {{
        modalContent.innerHTML = sanitizeAiHtml(cache.text);
        modalMeta.textContent = `${{cache.model || 'Gemini'}} · ${{cache.iso || ''}}`;
    }}
    document.getElementById('modalPeriodLabel').textContent = '· ' + (PERIOD_LABEL[period] || period);
}}

function renderCategoryBlock(category) {{
    const cache = AI_CACHE[category];
    const body = document.getElementById('catAiBody-' + category);
    const meta = document.getElementById('catAiMeta-' + category);
    const box = document.getElementById('catAi-' + category);
    if (!body) return;
    if (!cache || !cache.text) {{
        box.classList.add('empty');
        body.innerHTML = 'Разбор ещё не сгенерирован. Нажми ⟳ чтобы запросить у Gemini.';
        if (meta) meta.textContent = '';
        return;
    }}
    box.classList.remove('empty');
    body.textContent = extractSummary(cache.text);
    if (meta) meta.textContent = `${{cache.model || 'Gemini'}} · ${{formatAge(cache.iso)}}`;
}}

function renderAllCategories() {{
    CATEGORY_PERIODS.forEach(renderCategoryBlock);
}}

function switchPeriod(period) {{
    currentPeriod = period;
    document.querySelectorAll('.period-switch button').forEach(b => {{
        b.classList.toggle('active', b.dataset.period === period);
    }});
    renderAI(period);
}}
function openAiModal() {{
    document.getElementById('aiModal').classList.add('open');
    renderAI(currentPeriod);
}}
function openCategoryModal(category) {{
    currentPeriod = category;
    document.querySelectorAll('.period-switch button').forEach(b => {{
        b.classList.toggle('active', b.dataset.period === category);
    }});
    renderAI(category);
    document.getElementById('aiModal').classList.add('open');
}}
function closeAiModal() {{
    document.getElementById('aiModal').classList.remove('open');
}}

function openSymptomModal() {{
    document.getElementById('symptomModal').classList.add('open');
    setTimeout(() => {{
        const inp = document.getElementById('symptomInput');
        if (inp) inp.focus();
    }}, 100);
}}
function closeSymptomModal() {{
    document.getElementById('symptomModal').classList.remove('open');
}}

let currentShareWorkout = null;

function closeWorkoutShareModal() {{
    document.getElementById('workoutShareModal').classList.remove('open');
}}

function shareNumber(value, digits = 1) {{
    const n = Number(value);
    if (!Number.isFinite(n) || n <= 0) return '—';
    return n.toLocaleString('ru-RU', {{ maximumFractionDigits: digits }});
}}

function shareDuration(value) {{
    const min = Number(value);
    if (!Number.isFinite(min) || min <= 0) return '—';
    const rounded = Math.round(min);
    if (rounded >= 60) {{
        const h = Math.floor(rounded / 60);
        const m = rounded % 60;
        return m ? `${{h}} ч. ${{m}} мин.` : `${{h}} ч.`;
    }}
    return `${{rounded}} мин.`;
}}

function isGoogleFitWorkout(w) {{
    return (w?.source_package === 'com.google.android.apps.fitness')
        || String(w?.source_app || w?.source || '').toLowerCase().includes('google');
}}

function isOutdoorGoogleFitWorkout(w) {{
    const text = `${{w?.training || ''}} ${{w?.source_title || ''}}`.toLowerCase();
    return isGoogleFitWorkout(w) && !text.includes('treadmill') && !text.includes('дорож');
}}

function workoutDistanceKm(w) {{
    const km = Number(w.distance_km);
    if (Number.isFinite(km) && km > 0) return km;
    const m = Number(w.distance_m);
    if (Number.isFinite(m) && m > 0) return m / 1000;
    return 0;
}}

function seededRandom(seedText) {{
    let seed = 2166136261;
    String(seedText || 'workout').split('').forEach(ch => {{
        seed ^= ch.charCodeAt(0);
        seed = Math.imul(seed, 16777619);
    }});
    return function() {{
        seed += 0x6D2B79F5;
        let t = seed;
        t = Math.imul(t ^ (t >>> 15), t | 1);
        t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    }};
}}

function workoutGpsPoints(w) {{
    const containers = [
        w.route_points, w.route, w.locations, w.location_points,
        w.coordinates, w.track_points, w.geo_points, w.gps_points,
    ];
    for (const raw of containers) {{
        if (!Array.isArray(raw)) continue;
        const pts = raw.map(p => {{
            if (Array.isArray(p)) return {{ lat: Number(p[0]), lon: Number(p[1]) }};
            return {{
                lat: Number(p.lat ?? p.latitude),
                lon: Number(p.lon ?? p.lng ?? p.longitude),
            }};
        }}).filter(p => Number.isFinite(p.lat) && Number.isFinite(p.lon));
        if (pts.length >= 2) return pts;
    }}
    return [];
}}

function workoutHasGpsRoute(w) {{
    return workoutGpsPoints(w).length >= 2;
}}

function workoutRouteData(w) {{
    const points = workoutGpsPoints(w);
    return {{ gps: points.length >= 2, points }};
}}

function fitRoutePoints(route, width, height, pad) {{
    if (route.gps) {{
        const lats = route.points.map(p => p.lat);
        const lons = route.points.map(p => p.lon);
        const minLat = Math.min(...lats), maxLat = Math.max(...lats);
        const minLon = Math.min(...lons), maxLon = Math.max(...lons);
        const latSpan = maxLat - minLat || 1;
        const lonSpan = maxLon - minLon || 1;
        return route.points.map(p => ({{
            x: pad + ((p.lon - minLon) / lonSpan) * (width - pad * 2),
            y: pad + (1 - ((p.lat - minLat) / latSpan)) * (height - pad * 2),
        }}));
    }}
    return [];
}}

function drawRoad(ctx, pts, width, color) {{
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.beginPath();
    pts.forEach((p, i) => {{
        if (i === 0) ctx.moveTo(p.x, p.y);
        else ctx.lineTo(p.x, p.y);
    }});
    ctx.stroke();
    ctx.restore();
}}

function drawKmBubble(ctx, x, y, text) {{
    ctx.save();
    ctx.font = '700 12px -apple-system, Segoe UI, sans-serif';
    const tw = ctx.measureText(text).width;
    const bw = tw + 18;
    const bh = 25;
    const bx = Math.max(8, Math.min(x - bw / 2, ctx.canvas.width - bw - 8));
    const by = Math.max(8, y - 34);
    ctx.fillStyle = '#7db0ff';
    ctx.strokeStyle = 'rgba(40,83,150,0.22)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.roundRect(bx, by, bw, bh, 8);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = '#0d3367';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(text, bx + bw / 2, by + bh / 2 + 0.5);
    ctx.restore();
}}

function drawShareMap(canvas, workout) {{
    if (!canvas || !workout) return;
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(320, Math.round(rect.width || 420));
    const height = Math.max(240, Math.round(rect.height || 330));
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const bg = ctx.createLinearGradient(0, 0, width, height);
    bg.addColorStop(0, '#e7eef8');
    bg.addColorStop(1, '#d6e3f2');
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, width, height);

    const rnd = seededRandom('map-' + (workout.id || workout.run_id || workout.datetime || 'workout'));
    ctx.strokeStyle = 'rgba(118,143,173,0.24)';
    ctx.lineWidth = 1;
    for (let i = 0; i < 22; i++) {{
        const x = rnd() * width;
        const tilt = (rnd() - 0.5) * 80;
        ctx.beginPath();
        ctx.moveTo(x - tilt, -20);
        ctx.lineTo(x + tilt, height + 20);
        ctx.stroke();
    }}
    for (let i = 0; i < 16; i++) {{
        const y = rnd() * height;
        const tilt = (rnd() - 0.5) * 90;
        ctx.beginPath();
        ctx.moveTo(-20, y - tilt);
        ctx.lineTo(width + 20, y + tilt);
        ctx.stroke();
    }}
    ctx.strokeStyle = 'rgba(54,86,125,0.42)';
    ctx.lineWidth = 3;
    for (let i = 0; i < 4; i++) {{
        const y = height * (0.18 + rnd() * 0.65);
        ctx.beginPath();
        ctx.moveTo(-20, y);
        ctx.bezierCurveTo(width * 0.28, y + (rnd() - 0.5) * 90, width * 0.62, y + (rnd() - 0.5) * 90, width + 20, y + (rnd() - 0.5) * 80);
        ctx.stroke();
    }}
    ctx.fillStyle = 'rgba(119,190,145,0.30)';
    ctx.beginPath();
    ctx.ellipse(width * 0.76, height * 0.25, 54, 30, -0.4, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = 'rgba(89,141,211,0.34)';
    ctx.lineWidth = 4;
    ctx.beginPath();
    ctx.moveTo(-20, height * 0.68);
    ctx.bezierCurveTo(width * 0.28, height * 0.58, width * 0.58, height * 0.78, width + 20, height * 0.60);
    ctx.stroke();

    const route = workoutRouteData(workout);
    if (!route.gps) return;
    const pts = fitRoutePoints(route, width, height, 42);
    drawRoad(ctx, pts, 9, 'rgba(255,255,255,0.88)');
    drawRoad(ctx, pts, 5, '#1765d8');
    drawRoad(ctx, pts, 2, '#74a7ff');

    const start = pts[0];
    const end = pts[pts.length - 1];
    ctx.fillStyle = '#1765d8';
    ctx.strokeStyle = '#eaf2ff';
    ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.arc(start.x, start.y, 7, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(end.x, end.y, 9, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();

    const km = workoutDistanceKm(workout);
    const marks = Math.min(9, Math.floor(km));
    for (let n = 1; n <= marks; n++) {{
        const idx = Math.min(pts.length - 1, Math.max(0, Math.round((n / km) * (pts.length - 1))));
        drawKmBubble(ctx, pts[idx].x, pts[idx].y, `${{n}} км`);
    }}
}}

function shareWorkout(index) {{
    const workout = WORKOUTS[index];
    if (!workout) return;
    const route = workoutRouteData(workout);
    if (!route.gps) return;
    currentShareWorkout = workout;
    const fitOutdoor = isOutdoorGoogleFitWorkout(workout);
    const title = fitOutdoor && workout.source_title
        ? workout.source_title
        : (workout.training_ru || workout.training || 'Тренировка');
    const time = (workout.datetime || workout.date || '').replace('T', ' ');
    const source = workout.source_app || workout.source || workout.gateway || 'Health Connect';
    document.getElementById('shareTitle').textContent = title;
    document.getElementById('shareTime').textContent = [time, source].filter(Boolean).join(' · ');
    if (fitOutdoor) {{
        document.getElementById('shareDistance').textContent = shareDuration(workout.duration_min);
        document.getElementById('shareDistanceLabel').textContent = 'активное время';
        document.getElementById('shareDuration').textContent = shareNumber(workoutDistanceKm(workout), 2);
        document.getElementById('shareDurationLabel').textContent = 'км';
        document.getElementById('shareKcal').textContent = shareNumber(workout.heart_points, 0);
        document.getElementById('shareKcalLabel').textContent = 'баллы';
    }} else {{
        document.getElementById('shareDistance').textContent = shareNumber(workoutDistanceKm(workout), 2);
        document.getElementById('shareDistanceLabel').textContent = 'км';
        document.getElementById('shareDuration').textContent = shareNumber(workout.duration_min, 0);
        document.getElementById('shareDurationLabel').textContent = 'мин';
        document.getElementById('shareKcal').textContent = shareNumber(workout.kcal, 0);
        document.getElementById('shareKcalLabel').textContent = 'ккал';
    }}
    document.getElementById('shareMapBadge').textContent = 'GPS-маршрут';
    document.getElementById('shareNote').textContent =
        `Реальный GPS-маршрут${{workout.route_point_count ? ` (${{workout.route_point_count}} точек)` : ''}}.`;
    document.getElementById('workoutShareModal').classList.add('open');
    requestAnimationFrame(() => drawShareMap(document.getElementById('shareMapCanvas'), workout));
}}

async function copyShareText() {{
    if (!currentShareWorkout) return;
    const w = currentShareWorkout;
    const fitOutdoor = isOutdoorGoogleFitWorkout(w);
    const text = [
        fitOutdoor && w.source_title ? w.source_title : (w.training_ru || w.training || 'Тренировка'),
        (w.datetime || w.date || '').replace('T', ' '),
        workoutDistanceKm(w) ? `${{shareNumber(workoutDistanceKm(w), 2)}} км` : '',
        w.duration_min ? shareDuration(w.duration_min) : '',
        fitOutdoor && w.heart_points ? `${{shareNumber(w.heart_points, 0)}} баллы` : (w.kcal ? `${{shareNumber(w.kcal, 0)}} ккал` : ''),
    ].filter(Boolean).join(' · ');
    try {{
        await navigator.clipboard.writeText(text);
        toast('Текст тренировки скопирован', 'ok');
    }} catch (e) {{
        toast(text || 'Тренировка');
    }}
}}

function quickSymptom(text) {{
    const inp = document.getElementById('symptomInput');
    if (inp) inp.value = text;
    askSymptom();
}}
function renderMeasurements() {{
    const latest = document.getElementById('measurementsLatest');
    const history = document.getElementById('measurementsHistory');
    if (!latest) return;
    const dates = Object.keys(MEASUREMENTS).sort().reverse();
    if (!dates.length) {{
        latest.innerHTML = '<em style="color:var(--dim);">Замеров пока нет. Раскрой форму ниже и добавь первый — талия + бёдра минимум.</em>';
        history.innerHTML = '';
        return;
    }}
    const lastDate = dates[0];
    const last = MEASUREMENTS[lastDate];
    const prev = dates.length > 1 ? MEASUREMENTS[dates[1]] : null;

    // Latest row with deltas
    let html = `<div style="font-size:13px;color:var(--dim);margin-bottom:8px;">Последний замер: <strong style="color:var(--accent2);">${{lastDate}}</strong>${{prev ? ` · сравнение с ${{dates[1]}}` : ''}}</div>`;
    html += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;">';
    for (const [key, label] of MEASUREMENT_FIELDS) {{
        const v = last[key];
        if (v == null) continue;
        const pv = prev ? prev[key] : null;
        let delta = '';
        if (pv != null && pv !== v) {{
            const d = (v - pv).toFixed(1);
            const sign = d > 0 ? '+' : '';
            const cls = d < 0 ? 'green' : 'red';
            delta = `<span style="color:var(--${{cls === 'green' ? 'accent' : 'warn'}});font-size:11px;">${{sign}}${{d}}</span>`;
        }}
        html += `<div style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px 10px;">
            <div style="color:var(--dim);font-size:11px;">${{label}}</div>
            <div style="font-size:18px;font-weight:600;color:var(--text);">${{v}}<span style="font-size:11px;color:var(--dim);"> см</span> ${{delta}}</div>
        </div>`;
    }}
    html += '</div>';

    // Computed ratios (WHR, waist/height)
    if (last.waist_cm) {{
        const ratios = [];
        if (last.hips_cm) {{
            const whr = (last.waist_cm / last.hips_cm).toFixed(2);
            const whrLabel = whr < 0.95 ? '✓ норма' : (whr < 1.0 ? '⚠ повышенный риск' : '🚩 абдоминальное ожирение');
            ratios.push(`Талия/Бёдра (WHR): <strong>${{whr}}</strong> — ${{whrLabel}}`);
        }}
        // Height from user data (178 default for Alex)
        const height = (DATA.user && DATA.user.height_cm) || 178;
        const whtR = (last.waist_cm / height).toFixed(2);
        const whtLabel = whtR < 0.5 ? '✓ норма' : (whtR < 0.6 ? '⚠ повышенный кардиориск' : '🚩 высокий риск');
        ratios.push(`Талия/Рост: <strong>${{whtR}}</strong> — ${{whtLabel}}`);
        html += `<div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--border);font-size:12px;color:var(--muted);">${{ratios.join(' · ')}}</div>`;
    }}
    latest.innerHTML = html;

    // History (only if 2+)
    if (dates.length > 1) {{
        let h = '<details style="margin-top:8px;"><summary style="cursor:pointer;color:var(--dim);font-size:12px;padding:4px 0;">История (' + dates.length + ' дат)</summary>';
        h += '<div style="overflow-x:auto;margin-top:8px;"><table style="width:100%;font-size:12px;border-collapse:collapse;">';
        h += '<thead><tr style="border-bottom:1px solid var(--border);"><th style="text-align:left;padding:6px;color:var(--dim);">Дата</th>';
        for (const [, label] of MEASUREMENT_FIELDS) h += `<th style="text-align:right;padding:6px;color:var(--dim);">${{label}}</th>`;
        h += '</tr></thead><tbody>';
        for (const d of dates) {{
            h += `<tr><td style="padding:6px;color:var(--accent2);">${{d}}</td>`;
            for (const [k] of MEASUREMENT_FIELDS) {{
                const val = MEASUREMENTS[d][k];
                h += `<td style="padding:6px;text-align:right;">${{val != null ? val : '—'}}</td>`;
            }}
            h += '</tr>';
        }}
        h += '</tbody></table></div></details>';
        history.innerHTML = h;
    }} else {{
        history.innerHTML = '';
    }}
}}

async function saveMeasurements() {{
    if (!BRIDGE_ONLINE) {{
        toast('Замеры сохраняются только когда запущен dashboard_server.py', 'err');
        return;
    }}
    const date = document.getElementById('msrDate').value || new Date().toISOString().slice(0, 10);
    const payload = {{ date }};
    let count = 0;
    for (const [key] of MEASUREMENT_FIELDS) {{
        const inp = document.getElementById('msr_' + key);
        if (inp && inp.value !== '') {{
            payload[key] = parseFloat(inp.value);
            count++;
        }}
    }}
    if (count === 0) {{
        toast('Заполни хоть одно поле', 'err');
        return;
    }}
    try {{
        const r = await fetch(apiUrl('/api/measurements'), {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify(payload),
        }});
        const j = await r.json();
        if (!j.ok) throw new Error(j.error || 'fail');
        // обновляем локальный кэш и перерендериваем
        MEASUREMENTS[date] = j.entry;
        renderMeasurements();
        // очистить ввод
        for (const [key] of MEASUREMENT_FIELDS) {{
            const inp = document.getElementById('msr_' + key);
            if (inp) inp.value = '';
        }}
        toast(`Замер за ${{date}} сохранён (${{count}} полей)`, 'ok');
    }} catch (e) {{
        toast('Ошибка: ' + e.message, 'err');
    }}
}}

async function askSymptom() {{
    const inp = document.getElementById('symptomInput');
    const result = document.getElementById('symptomResult');
    const meta = document.getElementById('symptomMeta');
    const symptom = (inp.value || '').trim();
    if (!symptom) {{
        toast('Опиши симптом или нажми быструю кнопку', 'err');
        inp.focus();
        return;
    }}
    if (!BRIDGE_ONLINE) {{
        result.innerHTML = '<em>Симптом-режим работает только когда запущен dashboard_server.py (он стучится в Gemini в реалтайме). Запусти сервер и открой http://127.0.0.1:8787/</em>';
        return;
    }}
    result.innerHTML = '<div class="symptom-loading">⏳ Разбираю симптом, ищу похожие эпизоды и причины… это займёт 15-30 секунд.</div>';
    meta.textContent = '';
    try {{
        const r = await fetch(apiUrl('/api/symptom_acute'), {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ symptom }}),
        }});
        const j = await r.json();
        if (!j.ok) throw new Error(j.error || 'fail');
        result.innerHTML = sanitizeAiHtml(j.text);
        const cats = (j.categories || []).join(', ');
        meta.textContent = `${{j.model || 'gemini'}} · ${{j.iso || ''}} · похожих эпизодов в прошлом: ${{j.past_episodes_count}}${{cats ? ' · категории: ' + cats : ''}}`;
        toast('Разбор готов', 'ok');
    }} catch (e) {{
        result.innerHTML = '<em style="color:#ff8a8a;">Ошибка: ' + escapeHtml(e.message) + '</em>';
        toast('Ошибка: ' + e.message, 'err');
    }}
}}

async function refreshCategory(category) {{
    if (!BRIDGE_ONLINE) {{
        triggerBatRefresh('обновляю ' + (PERIOD_LABEL[category] || category));
        return;
    }}
    const body = document.getElementById('catAiBody-' + category);
    const meta = document.getElementById('catAiMeta-' + category);
    if (body) body.innerHTML = '<em>Генерирую…</em>';
    if (meta) meta.textContent = '…';
    try {{
        const r = await fetch(apiUrl('/api/ai/refresh'), {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ period: category }}),
        }});
        const j = await r.json();
        if (!j.ok) throw new Error(j.error || 'fail');
        AI_CACHE[category] = {{ text: j.text, iso: new Date().toISOString(), model: j.model || 'gemini' }};
        renderCategoryBlock(category);
        toast(`Разбор «${{PERIOD_LABEL[category] || category}}» готов`, 'ok');
    }} catch (e) {{
        toast('Ошибка: ' + e.message, 'err');
        renderCategoryBlock(category);
    }}
}}
function triggerBatRefresh(reason) {{
    // Оффлайн-режим: дёргаем кастомный URL-протокол healthrefresh:
    // (зарегистрирован в HKCU через register_url_protocol.reg).
    // Windows запускает auto_refresh.bat: sync → gemini → build_dashboard.
    toast('Запускаю auto_refresh.bat… ' + (reason || '') + ' Обновлю через ~30с.');
    window.location.href = 'healthrefresh:run';
    // через 30с перезагрузим страницу — батник уже перестроит HTML
    setTimeout(() => location.reload(), 30000);
}}

let LAST_RUNTIME_STATUS = null;

function sourceStateLabel(state) {{
    return {{
        fresh: 'свежее',
        cached: 'из кэша',
        stale: 'устарело',
        missing: 'нет данных',
    }}[state] || state || 'неизвестно';
}}

function formatSourceAge(hours) {{
    if (hours === null || hours === undefined) return 'дата неизвестна';
    if (hours < 1) return 'меньше часа назад';
    if (hours < 48) return `${{Math.round(hours)}} ч назад`;
    return `${{Math.round(hours / 24)}} дн назад`;
}}

function renderSourceHealth(status) {{
    LAST_RUNTIME_STATUS = status || {{}};
    const sources = Object.values(status.sources || {{}});
    const jobs = Object.values(status.jobs || {{}});
    const fresh = sources.filter(source => source.state === 'fresh').length;
    const usable = sources.filter(source => ['fresh', 'cached'].includes(source.state)).length;
    const attention = sources.some(source => ['stale', 'missing', 'cached'].includes(source.state));
    const running = jobs.filter(job => job.status === 'running');

    const button = document.getElementById('dataHealthBtn');
    const label = document.getElementById('dataHealthLabel');
    if (button) button.dataset.healthState = attention ? 'attention' : 'fresh';
    if (label) label.textContent = sources.length ? `${{usable}}/${{sources.length}} источников` : 'Данные';

    const summary = document.getElementById('dataHealthSummary');
    if (summary) {{
        summary.textContent = sources.length
            ? `Свежих источников: ${{fresh}} из ${{sources.length}}. Пригодны сейчас: ${{usable}}.`
            : 'Сервер доступен, но сведения об источниках пока не получены.';
    }}

    const list = document.getElementById('dataHealthSources');
    if (list) {{
        list.innerHTML = sources.map(source => `
            <div class="data-health-row">
                <div>
                    <div class="data-health-name">
                        <span class="source-state-dot ${{escapeHtml(source.state || 'missing')}}"></span>
                        ${{escapeHtml(source.label || 'Источник')}}
                    </div>
                    <div class="data-health-detail">
                        ${{Number(source.records || 0)}} записей · ${{escapeHtml(formatSourceAge(source.age_hours))}}
                    </div>
                </div>
                <div class="data-health-state">${{escapeHtml(sourceStateLabel(source.state))}}</div>
            </div>
        `).join('');
    }}

    const jobsBox = document.getElementById('dataHealthJobs');
    if (jobsBox) {{
        jobsBox.textContent = running.length
            ? `Сейчас выполняется: ${{running.map(job => job.name).join(', ')}}`
            : 'Фоновые задания не выполняются.';
    }}
}}

async function refreshRuntimeStatus() {{
    const response = await fetch(apiUrl('/api/status'), {{ cache: 'no-store' }});
    if (!response.ok) throw new Error('status unavailable');
    const status = await response.json();
    renderSourceHealth(status);
    return status;
}}

function toggleDataHealth() {{
    document.getElementById('dataHealthModal').classList.add('open');
    refreshRuntimeStatus().catch(() => {{
        const summary = document.getElementById('dataHealthSummary');
        if (summary) summary.textContent = 'Не удалось получить состояние источников.';
    }});
}}

function closeDataHealth() {{
    document.getElementById('dataHealthModal').classList.remove('open');
}}

async function refreshAI() {{
    if (!BRIDGE_ONLINE) {{
        triggerBatRefresh('обновляю AI');
        return;
    }}
    const btns = document.querySelectorAll('#refreshAiBtn, #modalRefreshBtn');
    btns.forEach(b => {{ b.disabled = true; b.textContent = '⟳ Генерация…'; }});
    const period = currentPeriod;
    toast('Генерирую анализ (' + period + ')…');
    try {{
        const r = await fetch(apiUrl('/api/ai/refresh'), {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ period }}),
        }});
        const j = await r.json();
        if (!j.ok) throw new Error(j.error || 'fail');
        AI_CACHE[period] = {{ text: j.text, iso: new Date().toISOString(), model: 'gemini-refresh' }};
        renderAI(period);
        toast(`Анализ готов (${{j.chars}} симв)`, 'ok');
    }} catch (e) {{
        toast('Ошибка: ' + e.message, 'err');
    }} finally {{
        btns.forEach(b => {{ b.disabled = false; b.innerHTML = '⟳ Обновить анализ'; }});
        document.getElementById('refreshAiBtn').textContent = '⟳';
    }}
}}

// --- Sync ---
async function doSync() {{
    if (!BRIDGE_ONLINE) {{
        triggerBatRefresh('синхронизация с телефоном');
        return;
    }}
    toast('Синхронизация с телефоном…');
    try {{
        const r = await fetch(apiUrl('/api/sync'), {{ method: 'POST' }});
        const j = await r.json();
        if (j.ok) {{
            toast(j.last || 'Синхронизировано', 'ok');
            setTimeout(() => location.reload(), 1200);
        }} else {{
            toast('Ошибка: ' + (j.error || 'fail'), 'err');
        }}
    }} catch (e) {{
        toast('Ошибка: ' + e.message, 'err');
    }}
}}

async function initServerBridge() {{
    try {{
        await refreshRuntimeStatus();
        BRIDGE_ONLINE = true;
        const composer = document.getElementById('composerCard');
        const hint = document.getElementById('composerOfflineHint');
        if (composer) composer.style.display = 'block';
        if (hint) hint.style.display = 'none';
    }} catch (e) {{
        BRIDGE_ONLINE = SERVER_MODE;
    }}
}}

// --- Notes ---
async function submitNote(replace) {{
    if (!BRIDGE_ONLINE) {{
        toast('Для записи заметки открой http://127.0.0.1:8787/ или запусти dashboard_server.py', 'err');
        return;
    }}
    const text = document.getElementById('noteText').value.trim();
    if (!text) {{ toast('Пустая заметка', 'err'); return; }}
    const date = document.getElementById('noteDate').value;
    const tagsRaw = document.getElementById('noteTags').value;
    const tags = tagsRaw.split(',').map(t => t.trim()).filter(Boolean);
    try {{
        const r = await fetch(apiUrl('/api/note'), {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ date, text, tags, replace }}),
        }});
        const j = await r.json();
        if (j.ok) {{
            toast('Сохранено ' + date, 'ok');
            document.getElementById('noteText').value = '';
            document.getElementById('noteTags').value = '';
            // Trigger sync to merge
            fetch(apiUrl('/api/sync'), {{ method: 'POST' }}).then(() => setTimeout(() => location.reload(), 800));
        }} else {{
            toast('Ошибка: ' + (j.error || '?'), 'err');
        }}
    }} catch (e) {{
        toast('Ошибка: ' + e.message, 'err');
    }}
}}

async function deleteNoteEntry(payload) {{
    if (!BRIDGE_ONLINE) {{
        toast('Удаление заметок доступно через http://127.0.0.1:8787/', 'err');
        return;
    }}
    const date = payload.date || '';
    const when = payload.time ? `, ${{payload.time}}` : '';
    if (!confirm(`Удалить PC-заметку за ${{date}}${{when}}?`)) return;
    try {{
        const r = await fetch(apiUrl('/api/note/delete'), {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify(payload),
        }});
        const j = await r.json();
        if (j.ok) {{
            toast('Удалено', 'ok');
            fetch(apiUrl('/api/sync'), {{ method: 'POST' }}).then(() => setTimeout(() => location.reload(), 800));
        }} else {{
            toast('Ошибка: ' + (j.error || '?'), 'err');
        }}
    }} catch (e) {{ toast('Ошибка: ' + e.message, 'err'); }}
}}

async function deleteNote(date) {{
    return deleteNoteEntry({{ date, all: true }});
}}

function editNote(date, existingText) {{
    document.getElementById('noteDate').value = date;
    document.getElementById('noteText').value = existingText || '';
    document.getElementById('noteText').focus();
    window.scrollTo({{ top: document.getElementById('composerCard').offsetTop - 20, behavior: 'smooth' }});
}}

// --- Charts ---
const chartOpts = {{
    responsive: true, maintainAspectRatio: false,
    interaction: {{ mode: 'index', intersect: false }},
    plugins: {{ legend: {{ position: 'bottom', labels: {{ padding: 10, boxWidth: 10, font: {{ size: 11 }} }} }} }},
}};

function mkChart(id, type, data, extra) {{
    const ctx = document.getElementById(id);
    if (!ctx) return;
    return new Chart(ctx, {{
        type, data,
        options: Object.assign({{}}, chartOpts, extra || {{}}),
    }});
}}

// Sleep stacked
mkChart('sleepChart', 'bar', {{
    labels: DATA.sleep.labels,
    datasets: [
        {{ label: 'Глубокий', data: DATA.sleep.deep, backgroundColor: '#6366f1', stack: 's', borderRadius: 4 }},
        {{ label: 'REM', data: DATA.sleep.rem, backgroundColor: '#a78bfa', stack: 's', borderRadius: 4 }},
        {{ label: 'Лёгкий', data: DATA.sleep.light, backgroundColor: '#60a5fa', stack: 's', borderRadius: 4 }},
        {{ label: 'Пробуждения', data: DATA.sleep.awake, backgroundColor: 'rgba(248,113,113,0.6)', stack: 's', borderRadius: 4 }},
    ],
}}, {{
    scales: {{
        x: {{ stacked: true, grid: {{ display: false }} }},
        y: {{ stacked: true, title: {{ display: true, text: 'минуты', color: '#5c6388' }} }},
    }},
}});

mkChart('sleepHrChart', 'line', {{
    labels: DATA.sleep.labels,
    datasets: [{{
        label: 'Ср. ЧСС',
        data: DATA.sleep.hr,
        borderColor: '#f87171',
        backgroundColor: 'rgba(248,113,113,0.12)',
        fill: true, tension: 0.35, borderWidth: 2, pointRadius: 2,
    }}],
}}, {{ plugins: {{ legend: {{ display: false }} }} }});

const lastIdx = DATA.sleep.labels.length - 1;
if (lastIdx >= 0) {{
    mkChart('stagesChart', 'doughnut', {{
        labels: ['Глубокий', 'REM', 'Лёгкий', 'Пробуждения'],
        datasets: [{{
            data: [DATA.sleep.deep[lastIdx], DATA.sleep.rem[lastIdx], DATA.sleep.light[lastIdx], DATA.sleep.awake[lastIdx]],
            backgroundColor: ['#6366f1', '#a78bfa', '#60a5fa', '#f87171'], borderWidth: 0,
        }}],
    }}, {{ cutout: '62%' }});
}}

mkChart('weightChart', 'line', {{
    labels: DATA.weight.labels,
    datasets: [{{
        label: 'Вес',
        data: DATA.weight.weight,
        borderColor: '#8b9eff',
        backgroundColor: 'rgba(139,158,255,0.15)',
        fill: true, tension: 0.25, borderWidth: 2, pointRadius: 2.5,
    }}],
}}, {{ plugins: {{ legend: {{ display: false }} }}, scales: {{ y: {{ title: {{ display: true, text: 'кг' }} }} }} }});

mkChart('bodyCompChart', 'line', {{
    labels: DATA.weight.labels,
    datasets: [
        {{ label: '% жира', data: DATA.weight.fat, borderColor: '#f472b6', backgroundColor: 'rgba(244,114,182,0.08)', yAxisID: 'y', tension: 0.3, pointRadius: 2, fill: true }},
        {{ label: '% мышц', data: DATA.weight.muscle, borderColor: '#4ade80', backgroundColor: 'rgba(74,222,128,0.08)', yAxisID: 'y1', tension: 0.3, pointRadius: 2, fill: true }},
    ],
}}, {{
    scales: {{
        y: {{ title: {{ display: true, text: '% жира' }}, grid: {{ color: 'rgba(244,114,182,0.05)' }} }},
        y1: {{ position: 'right', title: {{ display: true, text: '% мышц' }}, grid: {{ display: false }} }},
    }},
}});

const goalKcal = DATA.nutrition.goal_kcal;
const hasEnergyBalance = Boolean(DATA.nutrition.balance_labels && DATA.nutrition.balance_labels.length);
const balanceCard = document.querySelector('.nutrition-balance');
const calorieCard = document.querySelector('.nutrition-calories');
const kcalChartHint = document.getElementById('kcalChartHint');
if (hasEnergyBalance) {{
    mkChart('energyBalanceChart', 'bar', {{
        labels: DATA.nutrition.balance_labels,
        datasets: [
            {{
                label: 'Осталось / перебор',
                data: DATA.nutrition.balance_remaining,
                backgroundColor: DATA.nutrition.balance_remaining.map(v => v >= 0 ? 'rgba(74,222,128,0.72)' : 'rgba(248,113,113,0.78)'),
                borderRadius: 5,
                yAxisID: 'y',
            }},
            {{
                label: 'Зачтено активности',
                type: 'line',
                data: DATA.nutrition.balance_exercise_credit,
                borderColor: '#60a5fa',
                backgroundColor: 'rgba(96,165,250,0.10)',
                tension: 0.25,
                pointRadius: 2,
                yAxisID: 'y1',
            }},
            {{
                label: 'Базовая цель',
                type: 'line',
                data: DATA.nutrition.balance_base_goal,
                borderColor: 'rgba(245,195,107,0.75)',
                borderDash: [5, 5],
                pointRadius: 0,
                yAxisID: 'y2',
            }},
        ],
    }}, {{
        scales: {{
            y: {{ title: {{ display: true, text: 'остаток / перебор, ккал' }}, grid: {{ color: ctx => ctx.tick.value === 0 ? 'rgba(238,244,238,0.22)' : 'rgba(238,244,238,0.06)' }} }},
            y1: {{ position: 'right', title: {{ display: true, text: 'активность' }}, grid: {{ display: false }} }},
            y2: {{ display: false }},
        }},
    }});
}} else {{
    if (balanceCard) {{
        balanceCard.classList.add('is-unavailable');
        balanceCard.hidden = true;
    }}
    if (calorieCard) calorieCard.classList.add('is-wide');
    if (kcalChartHint) kcalChartHint.textContent = `${{DATA.nutrition.labels.length}} записанных дней · цель не задана`;
}}

mkChart('kcalChart', 'bar', {{
    labels: DATA.nutrition.labels,
    datasets: [{{
        label: 'ккал',
        data: DATA.nutrition.kcal,
        backgroundColor: DATA.nutrition.kcal.map(v => {{
            if (goalKcal && v > goalKcal * 1.1) return 'rgba(248,113,113,0.75)';
            if (goalKcal && v < goalKcal * 0.75) return 'rgba(251,191,36,0.6)';
            return goalKcal ? 'rgba(74,222,128,0.7)' : 'rgba(45,212,191,0.68)';
        }}),
        borderRadius: 5,
    }}],
}}, {{
    plugins: {{ legend: {{ display: false }} }},
    scales: {{ y: {{ title: {{ display: true, text: 'ккал' }} }} }},
}});

mkChart('macrosChart', 'bar', {{
    labels: DATA.nutrition.labels,
    datasets: [
        {{ label: 'Белки', data: DATA.nutrition.protein, backgroundColor: '#4ade80', stack: 'm', borderRadius: 4 }},
        {{ label: 'Жиры', data: DATA.nutrition.fat, backgroundColor: '#fbbf24', stack: 'm', borderRadius: 4 }},
        {{ label: 'Углеводы', data: DATA.nutrition.carb, backgroundColor: '#60a5fa', stack: 'm', borderRadius: 4 }},
    ],
}}, {{
    scales: {{
        x: {{ stacked: true, grid: {{ display: false }} }},
        y: {{ stacked: true, title: {{ display: true, text: 'грамм' }} }},
    }},
}});

mkChart('stepsChart', 'bar', {{
    labels: DATA.activity.labels,
    datasets: [
        {{ label: 'Шаги', data: DATA.activity.steps, backgroundColor: 'rgba(139,158,255,0.65)', borderRadius: 5, yAxisID: 'y' }},
        {{ label: 'Акт. мин', type: 'line', data: DATA.activity.active_min, borderColor: '#fbbf24', backgroundColor: 'rgba(251,191,36,0.15)', yAxisID: 'y1', tension: 0.35, pointRadius: 2, fill: true }},
        {{ label: 'Тренировки мин', type: 'line', data: DATA.activity.workout_min, borderColor: '#7dd3c7', backgroundColor: 'rgba(125,211,199,0.10)', yAxisID: 'y1', tension: 0.25, pointRadius: 2, fill: false }},
    ],
}}, {{
    scales: {{
        y: {{ title: {{ display: true, text: 'шаги' }} }},
        y1: {{ position: 'right', title: {{ display: true, text: 'мин' }}, grid: {{ display: false }} }},
    }},
}});

const workoutDiv = document.getElementById('workoutList');
let workoutVisibleCount = 12;
let workoutFilter = 'all';
if (workoutDiv) {{
    if (WORKOUTS && WORKOUTS.length) {{
        const ZONE_COLORS = {{ z1: '#4ade80', z2: '#a3e635', z3: '#fbbf24', z4: '#fb923c', z5: '#f87171' }};
        const ZONE_LABELS = {{ z1: 'Recovery 50-60%', z2: 'Aerobic 60-70%', z3: 'Tempo 70-80%', z4: 'Threshold 80-90%', z5: 'VO₂max 90-100%' }};
        function renderWorkoutCards() {{
            workoutDiv.innerHTML = '';
            const mapCount = WORKOUTS.filter(workoutHasGpsRoute).length;
            const entries = WORKOUTS
                .map((w, idx) => ({{ w, idx }}))
                .filter(item => workoutFilter === 'maps' ? workoutHasGpsRoute(item.w) : true);
            const shown = entries.slice(0, workoutVisibleCount);
            const controls = document.createElement('div');
            controls.className = 'workout-controls';
            controls.innerHTML = `
                <div class="workout-filter">
                    <button type="button" class="btn sm ${{workoutFilter === 'all' ? 'active' : ''}}" data-workout-filter="all">Все</button>
                    <button type="button" class="btn sm ${{workoutFilter === 'maps' ? 'active' : ''}}" data-workout-filter="maps">С картой ${{mapCount}}</button>
                    <button type="button" class="btn sm" data-workout-show-all>Показать все</button>
                </div>
                <div class="workout-count">Показано ${{shown.length}} из ${{entries.length}} · всего ${{WORKOUTS.length}}</div>
            `;
            workoutDiv.appendChild(controls);
        shown.forEach(({{ w, idx }}) => {{
            const el = document.createElement('div');
            el.className = 'workout-card';
            const titleRu = w.training_ru || w.training || 'Тренировка';
            const distHtml = w.distance_km ? `<div class="metric"><strong>${{w.distance_km}}</strong>км</div>` : '';
            const speedHtml = w.avg_speed_kmh ? `<div class="metric"><strong>${{w.avg_speed_kmh}}</strong>км/ч</div>` : '';
            const hrHtml = w.hr_avg ? `<div class="metric"><strong>${{w.hr_avg}}</strong>ср ЧСС${{w.hr_max ? ' / ' + w.hr_max + ' max' : ''}}</div>` : '';
            const hpHtml = w.heart_points ? `<div class="metric" title="Google Heart Points: 1pt/мин при 50-70% max HR, 2pt/мин при ≥70%"><strong>${{w.heart_points}}</strong>HP</div>` : '';
            const trimpHtml = w.trimp ? `<div class="metric" title="Edwards' TRIMP — общая нагрузка тренировки"><strong>${{w.trimp}}</strong>TRIMP</div>` : '';
            const elevHtml = w.elevation_gain_m ? `<div class="metric"><strong>${{w.elevation_gain_m}}</strong>м ↑</div>` : '';
            const fragmentHtml = w.fragment_count ? ` · ${{w.fragment_count}} фрагм.` : '';
            const sourceBits = [];
            if (w.source_app) sourceBits.push(w.source_app);
            if (w.source && w.source !== w.source_app) sourceBits.push(w.source);
            if (w.gateway && !sourceBits.includes(w.gateway)) sourceBits.push(w.gateway);
            const sourceLine = sourceBits.length ? sourceBits.join(' через ') : 'Health Connect';
            // Home Workouts breakdown — показываем количество упражнений и среднюю длительность
            const hwHtml = (w.exercises_count && w.exercises_avg_sec) ? `<div class="metric" title="Home Workouts: ${{w.exercises_count}} упражнений по ${{w.exercises_avg_sec}}с в среднем"><strong>${{w.exercises_count}}×${{Math.round(w.exercises_avg_sec)}}с</strong>упр</div>` : '';

            // Зоны — стэк-бар внизу карточки
            let zonesHtml = '';
            const zones = w.zones_min || {{}};
            const totalZ = ['z1','z2','z3','z4','z5'].reduce((s,k) => s + (zones[k] || 0), 0);
            if (totalZ > 0.5) {{
                const segs = ['z1','z2','z3','z4','z5'].map(k => {{
                    const m = zones[k] || 0;
                    if (m < 0.1) return '';
                    const pct = (m / totalZ) * 100;
                    return `<div class="zone-seg" title="${{ZONE_LABELS[k]}}: ${{m.toFixed(1)}} мин" style="background:${{ZONE_COLORS[k]}};width:${{pct}}%;"></div>`;
                }}).join('');
                zonesHtml = `<div class="zone-bar">${{segs}}</div>`;
            }}

            // KS Fit native series → expandable chart
            const series = Array.isArray(w.ksfit_series) ? w.ksfit_series : [];
            const hasSeries = series.length >= 3;
            const seriesId = `ksfitChart_${{(w.id || w.run_id || Math.random().toString(36).slice(2)).toString().replace(/[^A-Za-z0-9_]/g,'_')}}`;
            const spmHtml = w.spm_avg ? `<div class="metric" title="Средний каденс (шагов/мин), KS Fit"><strong>${{w.spm_avg}}</strong>spm</div>` : '';
            const seriesMeta = hasSeries
                ? `<div class="metric ks-toggle" data-target="${{seriesId}}" title="Открыть детальный профиль тренировки"><strong>📈</strong>график</div>`
                : '';
            const chartHtml = hasSeries
                ? `<div class="ks-chart-wrap" id="${{seriesId}}" style="display:none;"><canvas></canvas></div>`
                : '';
            const shareHtml = workoutHasGpsRoute(w)
                ? `<button type="button" class="btn sm workout-share-btn" data-workout-index="${{idx}}" title="Открыть GPS-карту">Карта</button>`
                : '';

            el.innerHTML = `
                <div class="workout-row">
                    <div>
                        <div class="name">${{escapeHtml(titleRu)}}</div>
                        <div class="src">${{escapeHtml((w.datetime || w.date || '').replace('T', ' '))}} · ${{escapeHtml(sourceLine)}}${{escapeHtml(fragmentHtml)}}</div>
                    </div>
                    <div class="metric"><strong>${{escapeHtml(w.duration_min || 0)}}</strong>мин</div>
                    <div class="metric"><strong>${{escapeHtml(w.kcal || 0)}}</strong>ккал</div>
                    ${{w.steps ? `<div class="metric"><strong>${{w.steps}}</strong>шаги</div>` : ''}}
                    ${{distHtml}}
                    ${{speedHtml}}
                    ${{spmHtml}}
                    ${{hrHtml}}
                    ${{hpHtml}}
                    ${{trimpHtml}}
                    ${{elevHtml}}
                    ${{hwHtml}}
                    ${{seriesMeta}}
                    ${{shareHtml}}
                </div>
                ${{zonesHtml}}
                ${{chartHtml}}
            `;
            if (hasSeries) {{
                el.dataset.ksSeries = JSON.stringify(series);
            }}
            workoutDiv.appendChild(el);
        }});
        }}

        renderWorkoutCards();

        workoutDiv.addEventListener('click', (ev) => {{
            const filterBtn = ev.target.closest('[data-workout-filter]');
            if (filterBtn) {{
                workoutFilter = filterBtn.dataset.workoutFilter || 'all';
                workoutVisibleCount = workoutFilter === 'maps' ? WORKOUTS.length : 12;
                renderWorkoutCards();
                return;
            }}
            const showAllBtn = ev.target.closest('[data-workout-show-all]');
            if (showAllBtn) {{
                workoutVisibleCount = WORKOUTS.length;
                renderWorkoutCards();
                return;
            }}
            const shareBtn = ev.target.closest('.workout-share-btn');
            if (shareBtn) {{
                shareWorkout(Number(shareBtn.dataset.workoutIndex));
                return;
            }}
            const toggle = ev.target.closest('.ks-toggle');
            if (!toggle) return;
            const card = toggle.closest('.workout-card');
            const targetId = toggle.dataset.target;
            const wrap = card && card.querySelector(`#${{targetId}}`);
            if (!wrap) return;
            const open = wrap.style.display !== 'none';
            wrap.style.display = open ? 'none' : 'block';
            if (open) return;
            if (wrap.dataset.rendered === '1') return;
            const raw = card.dataset.ksSeries;
            if (!raw) return;
            const pts = JSON.parse(raw);
            const labels = pts.map(p => Math.round((p.t_s || 0) / 6) / 10);  // minutes
            const speed = pts.map(p => (p.speed_kmh ?? null));
            const spm = pts.map(p => (p.spm ?? null));
            const hrSeries = pts.map(p => p.hr ?? null);
            const hasHr = hrSeries.some(v => v && v > 0);
            const datasets = [
                {{ label: 'Скорость, км/ч', data: speed, borderColor: '#8b9eff', backgroundColor: 'rgba(139,158,255,0.18)', tension: 0.3, pointRadius: 2, fill: true, yAxisID: 'y' }},
                {{ label: 'Каденс, шаг/мин', data: spm, borderColor: '#7dd3c7', tension: 0.3, pointRadius: 2, yAxisID: 'y1', fill: false }},
            ];
            if (hasHr) {{
                datasets.push({{ label: 'ЧСС', data: hrSeries, borderColor: '#f87171', borderDash: [4,4], tension: 0.3, pointRadius: 1, yAxisID: 'y1' }});
            }}
            const canvas = wrap.querySelector('canvas');
            new Chart(canvas.getContext('2d'), {{
                type: 'line',
                data: {{ labels, datasets }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: {{ mode: 'index', intersect: false }},
                    plugins: {{ legend: {{ labels: {{ color: '#cbd5e1' }} }} }},
                    scales: {{
                        x: {{ title: {{ display: true, text: 'минуты' }}, ticks: {{ color: '#94a3b8' }}, grid: {{ color: 'rgba(255,255,255,0.04)' }} }},
                        y: {{ title: {{ display: true, text: 'км/ч' }}, ticks: {{ color: '#94a3b8' }}, grid: {{ color: 'rgba(255,255,255,0.04)' }} }},
                        y1: {{ position: 'right', title: {{ display: true, text: 'spm / bpm' }}, ticks: {{ color: '#94a3b8' }}, grid: {{ display: false }} }},
                    }},
                }},
            }});
            wrap.dataset.rendered = '1';
        }});
    }} else {{
        workoutDiv.innerHTML = '<div class="empty">Тренировки пока не найдены в синхронизации.</div>';
    }}
}}

mkChart('dailyHrChart', 'line', {{
    labels: DATA.health.labels,
    datasets: [
        {{ label: 'Мин', data: DATA.health.hr_min, borderColor: 'rgba(96,165,250,0.7)', fill: '+1', backgroundColor: 'rgba(96,165,250,0.08)', tension: 0.3, pointRadius: 1.5 }},
        {{ label: 'Средн', data: DATA.health.hr_avg, borderColor: '#8b9eff', borderWidth: 2, tension: 0.3, pointRadius: 2 }},
        {{ label: 'Макс', data: DATA.health.hr_max, borderColor: '#f87171', tension: 0.3, pointRadius: 1.5 }},
    ],
}});

mkChart('stressChart', 'line', {{
    labels: DATA.health.labels,
    datasets: [
        {{ label: 'Ср', data: DATA.health.stress_avg, borderColor: '#fbbf24', backgroundColor: 'rgba(251,191,36,0.12)', fill: true, tension: 0.35, pointRadius: 2 }},
        {{ label: 'Макс', data: DATA.health.stress_max, borderColor: '#f87171', borderDash: [4,4], tension: 0.35, pointRadius: 1.5 }},
    ],
}});

mkChart('spo2Chart', 'line', {{
    labels: DATA.health.labels,
    datasets: [{{
        label: 'SpO₂',
        data: DATA.health.spo2_avg,
        borderColor: '#4ade80',
        backgroundColor: 'rgba(74,222,128,0.12)',
        fill: true, tension: 0.35, pointRadius: 2,
    }}],
}}, {{ plugins: {{ legend: {{ display: false }} }}, scales: {{ y: {{ min: 90, max: 100 }} }} }});

// --- Meals ---
const mealMeta = {{
    breakfast: {{ label: 'Завтрак', icon: 'icon-sunrise' }},
    lunch: {{ label: 'Обед', icon: 'icon-sun' }},
    dinner: {{ label: 'Ужин', icon: 'icon-sleep' }},
    snack: {{ label: 'Перекус', icon: 'icon-apple' }},
    other: {{ label: 'Другое', icon: 'icon-nutrition' }},
}};
let selectedNutritionDay = 0;
const nutritionDaysData = (NUTRITION_DIARY && Array.isArray(NUTRITION_DIARY.days)) ? NUTRITION_DIARY.days : [];
function signedKcal(value) {{
    const v = Math.round(value || 0);
    return (v > 0 ? '+' : '') + v + ' ккал';
}}
function deltaClass(value) {{
    return (value || 0) >= 0 ? 'ok' : 'bad';
}}
function macroBar(label, value, goal) {{
    const g = Math.round(goal || 0);
    const v = Math.round(value || 0);
    const pct = g ? Math.min(130, Math.round(v / g * 100)) : 0;
    const cls = pct > 115 ? 'bad' : pct > 100 ? 'warn' : '';
    return `<div class="macro-line${{g ? '' : ' no-goal'}}">
        <div class="top"><span>${{label}}</span><span>${{v}}${{g ? ` / ${{g}}` : ''}} г</span></div>
        ${{g ? `<div class="macro-track"><div class="macro-fill ${{cls}}" style="--pct:${{pct}}%;"></div></div>` : ''}}
    </div>`;
}}
function currentNutritionDay() {{
    return nutritionDaysData[selectedNutritionDay] || NUTRITION_DIARY.today || {{}};
}}
function clampNutritionIndex(index) {{
    if (!nutritionDaysData.length) return 0;
    return Math.max(0, Math.min(nutritionDaysData.length - 1, index));
}}
function renderNutritionMeta(day) {{
    const title = document.getElementById('nutritionSelectedDate');
    const sub = document.getElementById('nutritionSelectedMeta');
    const hint = document.getElementById('nutritionHeaderHint');
    const prev = document.getElementById('nutritionPrevDay');
    const next = document.getElementById('nutritionNextDay');
    if (title) title.textContent = day.date || 'нет данных';
    if (hint) hint.textContent = day.date || '';
    if (sub) {{
        const budget = Math.round(day.adjusted_goal || day.base_goal || 0);
        const remaining = Math.round(day.remaining || 0);
        const balanceText = remaining >= 0 ? `${{remaining}} ккал в запасе` : `${{Math.abs(remaining)}} ккал сверх бюджета`;
        const hasCalorieGoal = Boolean(day.adjusted_goal || day.base_goal);
        sub.textContent = !day.date
            ? 'нет данных о питании'
            : hasCalorieGoal
                ? `${{selectedNutritionDay + 1}} из ${{nutritionDaysData.length}} · съедено ${{day.total_kcal || 0}} / ${{budget}} ккал · ${{balanceText}} с активностью`
                : `${{selectedNutritionDay + 1}} из ${{nutritionDaysData.length}} · съедено ${{day.total_kcal || 0}} ккал · цель не задана`;
    }}
    if (prev) prev.disabled = !nutritionDaysData.length || selectedNutritionDay >= nutritionDaysData.length - 1;
    if (next) next.disabled = !nutritionDaysData.length || selectedNutritionDay <= 0;
}}
function renderNutritionSummary(day) {{
    const box = document.getElementById('nutritionTodaySummary');
    if (!box) return;
    if (!day || !day.date) {{
        box.innerHTML = '<div class="empty">Нет данных о питании</div>';
        return;
    }}
    const remaining = Math.round(day.remaining || 0);
    const over = Math.max(0, Math.round(day.over_adjusted || 0));
    const gaugeValue = remaining >= 0 ? remaining : over;
    const gaugeLabel = remaining >= 0 ? 'можно ещё' : 'сверх бюджета';
    const gaugeColor = remaining >= 0 ? '#4ade80' : '#ff6f61';
    const pct = day.adjusted_goal ? Math.min(100, Math.round((day.total_kcal || 0) / day.adjusted_goal * 100)) : 0;
    const goals = day.macro_goals || {{}};
    const hasCalorieGoal = Boolean(day.adjusted_goal || day.base_goal);
    const goalState = hasCalorieGoal
        ? `<div class="diary-gauge" style="--gauge-pct:${{pct}}%;--gauge-color:${{gaugeColor}};">
            <div class="inside">
                <div class="big">${{gaugeValue}}</div>
                <div class="small">${{gaugeLabel}}</div>
            </div>
        </div>`
        : `<div class="diary-goal-state">
            <div class="metric-head">
                <svg class="ui-icon"><use href="#icon-chart-donut"></use></svg>
                Баланс
            </div>
            <div class="goal-title">Цель не задана</div>
            <div class="cap">Баланс и остаток не рассчитываются без нормы калорий.</div>
        </div>`;
    const budgetState = hasCalorieGoal
        ? `<div class="energy-card ${{remaining >= 0 ? 'ok' : 'bad'}}">
            <div class="label">Бюджет с активностью</div>
            <div class="value">${{day.total_kcal || 0}} / ${{day.adjusted_goal || day.base_goal}}<span class="u">ккал</span></div>
            <div class="note">к базовой цели ${{signedKcal(day.base_delta || 0)}} · активность +${{day.exercise_credit || 0}} · с активностью ${{signedKcal(day.adjusted_delta || 0)}}</div>
        </div>`
        : `<div class="energy-card warn">
            <div class="label">Цель калорий не задана</div>
            <div class="value">${{day.total_kcal || 0}}<span class="u">ккал записано</span></div>
            <div class="note">Показываем фактическое питание без оценки дефицита или перебора.</div>
        </div>`;
    box.innerHTML = `<div class="diary-summary">
        <div class="diary-overview">
            <div class="diary-metric">
                <div class="metric-head">
                    <svg class="ui-icon"><use href="#icon-fire"></use></svg>
                    Съедено
                </div>
                <div class="num">${{day.total_kcal || 0}}<span class="u">ккал</span></div>
                <div class="cap">Все записанные приёмы пищи за день</div>
            </div>
            <div class="diary-metric">
                <div class="metric-head">
                    <svg class="ui-icon"><use href="#icon-activity"></use></svg>
                    Активность
                </div>
                <div class="num">${{day.exercise_raw || 0}}<span class="u">ккал</span></div>
                <div class="cap">${{day.exercise_raw ? 'Расход по данным активности' : 'Нет расхода за выбранный день'}}</div>
            </div>
            ${{goalState}}
        </div>
        <div class="macro-bars">
            ${{macroBar('Углеводы', day.carb_g, goals.carb_g)}}
            ${{macroBar('Белки', day.protein_g, goals.protein_g)}}
            ${{macroBar('Жиры', day.fat_g, goals.fat_g)}}
            ${{budgetState}}
        </div>
    </div>`;
}}
function renderNutritionMeals(day) {{
    const mealsDiv = document.getElementById('mealsGrid');
    if (!mealsDiv) return;
    mealsDiv.innerHTML = '';
    const meals = (day && Array.isArray(day.meals)) ? day.meals : [];
    if (!meals.length) {{
        mealsDiv.innerHTML = '<div class="empty">Нет записанных приёмов пищи за этот день</div>';
        return;
    }}
    for (const info of meals) {{
        const card = document.createElement('div');
        const meal = info.key || 'other';
        const meta = mealMeta[meal] || mealMeta.other;
        card.className = 'meal ' + meal;
        const items = (info.items || []).map(i => `<div>• ${{escapeHtml(i.name)}} — ${{i.amount_g}}г (${{i.kcal}})</div>`).join('');
        const target = info.target ? ` / ${{info.target}}` : '';
        const delta = info.delta == null ? '' : `<span class="${{info.delta > 0 ? 'bad' : 'ok'}}">${{signedKcal(info.delta)}}</span>`;
        card.innerHTML = `
            <div class="m-name"><svg class="ui-icon"><use href="#${{meta.icon}}"></use></svg>${{meta.label}}</div>
            <div class="m-kcal">${{info.kcal}}${{target}}<span class="u"> ккал</span></div>
            <div class="m-macros">Б ${{info.protein_g}} · Ж ${{info.fat_g}} · У ${{info.carb_g}}</div>
            <div class="m-macros">${{delta}}</div>
            <div class="m-items">${{items}}</div>
        `;
        mealsDiv.appendChild(card);
    }}
}}
function renderNutritionRollups() {{
    const rollupsDiv = document.getElementById('nutritionRollups');
    if (!rollupsDiv || !NUTRITION_DIARY.rollups) return;
    rollupsDiv.innerHTML = '';
    for (const [key, label] of [['week', '7 дней'], ['month', '30 дней']]) {{
        const r = NUTRITION_DIARY.rollups[key] || {{}};
        const rest = Math.round(r.remaining || 0);
        const avg = Math.round(r.avg_remaining || 0);
        const overDays = Math.round(r.over_days || 0);
        const days = Math.round(r.days || 0);
        const title = rest >= 0 ? 'запас по записанным данным' : 'перебор по бюджету';
        const el = document.createElement('div');
        el.className = 'diary-rollup';
        el.innerHTML = `
            <div class="label">${{label}}</div>
            <div class="value ${{deltaClass(rest)}}">${{signedKcal(rest)}}</div>
            <div class="note">${{title}} · в среднем ${{signedKcal(avg)}}/день · перебор дней ${{overDays}}/${{days}}</div>
            <div class="note">активность зачтена +${{Math.round(r.exercise_credit || 0)}} ккал · к базовой цели ${{signedKcal(r.base_delta || 0)}}</div>
        `;
        rollupsDiv.appendChild(el);
    }}
}}
function renderNutritionDayStrip() {{
    const daysDiv = document.getElementById('nutritionDays');
    if (!daysDiv) return;
    daysDiv.innerHTML = '';
    if (!nutritionDaysData.length) return;
    nutritionDaysData.slice(0, 14).forEach((d, index) => {{
        const rest = Math.round(d.remaining || 0);
        const hasCalorieGoal = Boolean(d.adjusted_goal || d.base_goal);
        const el = document.createElement('button');
        el.type = 'button';
        el.className = 'nutrition-day' + (index === selectedNutritionDay ? ' active' : '');
        el.addEventListener('click', () => selectNutritionDay(index));
        el.innerHTML = `
            <div class="date">${{escapeHtml(d.date || '')}}</div>
            <div class="line">${{d.total_kcal || 0}}${{hasCalorieGoal ? ` / ${{d.adjusted_goal || d.base_goal}}` : ''}} ккал</div>
            ${{hasCalorieGoal
                ? `<div class="line delta ${{deltaClass(rest)}}">${{signedKcal(rest)}}</div>`
                : '<div class="line">цель не задана</div>'}}
        `;
        daysDiv.appendChild(el);
    }});
}}
function renderNutritionDiary() {{
    selectedNutritionDay = clampNutritionIndex(selectedNutritionDay);
    const day = currentNutritionDay();
    renderNutritionMeta(day);
    renderNutritionSummary(day);
    renderNutritionMeals(day);
    renderNutritionRollups();
    renderNutritionDayStrip();
}}
function selectNutritionDay(index) {{
    selectedNutritionDay = clampNutritionIndex(index);
    renderNutritionDiary();
}}
function shiftNutritionDay(delta) {{
    selectNutritionDay(selectedNutritionDay + delta);
}}
renderNutritionDiary();

// --- Notes rendering ---
const notesDiv = document.getElementById('notesSection');
if (FEELINGS && FEELINGS.length) {{
    FEELINGS.forEach(f => {{
        const date = (f.date || '').replace(/"/g, '');
        const hasY = !!f.yazio_note;
        const manualEntries = (Array.isArray(f.manual_entries) && f.manual_entries.length)
            ? f.manual_entries
            : (f.manual_note ? [{{ text: f.manual_note, time: '', tags: f.manual_tags || [], added_at: null, index: null }}] : []);
        const hasM = manualEntries.length > 0;
        const cls = hasY && hasM ? 'has-both' : hasY ? 'has-yazio' : 'has-manual';

        const card = document.createElement('div');
        card.className = 'note-card ' + cls;

        const chips = [];
        if (hasY) chips.push('<span class="chip yazio">YAZIO</span>');
        if (hasM) chips.push('<span class="chip manual">С ПК</span>');

        let body = '';
        if (hasY) {{
            const yTags = (f.yazio_tags || []).map(t => `<span class="tag">${{escapeHtml(t)}}</span>`).join('');
            body += `<div class="note-section">
                <div class="section-label">💛 YAZIO</div>
                <div class="note-body">${{escapeHtml(f.yazio_note)}}</div>
                ${{yTags ? `<div class="note-tags">${{yTags}}</div>` : ''}}
            </div>`;
        }}
        if (hasM) {{
            const manualHtml = manualEntries.map((entry, idx) => {{
                const entryTags = (entry.tags || []).map(t => `<span class="tag">${{escapeHtml(t)}}</span>`).join('');
                const time = entry.time ? `[${{escapeHtml(entry.time)}}] ` : '';
                const payload = {{
                    date,
                    added_at: entry.added_at || null,
                    index: entry.index !== undefined && entry.index !== null ? entry.index : idx,
                    time: entry.time || '',
                }};
                return `<div class="manual-entry">
                    <div class="manual-entry-main">
                        <div class="note-body">${{time}}${{escapeHtml(entry.text || '')}}</div>
                        ${{entryTags ? `<div class="note-tags">${{entryTags}}</div>` : ''}}
                    </div>
                    <button class="btn sm note-delete-btn" title="Удалить эту PC-заметку" onclick='deleteNoteEntry(${{JSON.stringify(payload)}})'>🗑</button>
                </div>`;
            }}).join('');
            body += `<div class="note-section">
                <div class="section-label">💜 С ПК</div>
                ${{manualHtml}}
            </div>`;
        }}

        const actions = '';

        card.innerHTML = `
            ${{actions}}
            <div class="note-head">
                <div class="note-date">${{escapeHtml(date)}}</div>
                <div class="note-src">${{chips.join('')}}</div>
            </div>
            ${{body}}
        `;
        notesDiv.appendChild(card);
    }});
}} else {{
    notesDiv.innerHTML = '<div class="empty">Нет заметок</div>';
}}

// --- Useful food ideas ---
const ideasDiv = document.getElementById('foodIdeas');
if (ideasDiv) {{
    if (FOOD_IDEAS && FOOD_IDEAS.length) {{
        FOOD_IDEAS.forEach(m => {{
            const el = document.createElement('div');
            el.className = 'food-idea';
            el.innerHTML = `
                <div class="nm">${{escapeHtml(m.title)}}</div>
                <div class="meta">${{escapeHtml(m.priority || '')}}</div>
                <div class="cook"><strong>${{escapeHtml(m.dish || m.title)}}</strong></div>
                <div class="why">${{escapeHtml(m.reason || '')}}</div>
                <div class="cook">${{escapeHtml(m.idea || '')}}</div>
                ${{m.recipe ? `<div class="recipe">${{escapeHtml(m.recipe)}}</div>` : ''}}
            `;
            ideasDiv.appendChild(el);
        }});
    }} else {{
        ideasDiv.innerHTML = '<div class="empty" style="padding:20px;">Мало данных по питанию — после нескольких дней YAZIO тут появятся идеи блюд.</div>';
    }}
}}

// --- Food profile ---
function initFoodProfile() {{
    const profile = Object.assign({{}}, FOOD_PROFILE || {{}}, JSON.parse(localStorage.getItem('foodProfileDraft') || '{{}}'));
    const avoid = new Set(profile.avoid_groups || []);
    const preferred = new Set([...(profile.preferred_proteins || []), ...(profile.preferred_sides || []), ...(profile.preferred_vegetables || [])]);
    document.querySelectorAll('[data-avoid]').forEach(el => {{
        el.checked = avoid.has(el.dataset.avoid);
    }});
    document.querySelectorAll('[data-prefer]').forEach(el => {{
        el.checked = preferred.has(el.dataset.prefer);
    }});
    const notes = document.getElementById('foodProfileNotes');
    if (notes) notes.value = profile.notes || '';
    const recipeMode = document.getElementById('recipeMode');
    if (recipeMode) recipeMode.checked = profile.recipe_mode !== false;
    const status = document.getElementById('foodProfileStatus');
    if (status) status.textContent = profile.updated_at ? ('обновлён ' + profile.updated_at) : 'профиль по умолчанию';
    const deepStatus = document.getElementById('foodProfileDeepStatus');
    const answersCount = Object.keys(profile.survey_answers || {{}}).length;
    if (deepStatus) deepStatus.textContent = answersCount ? `${{answersCount}} ответов, цель: ${{profile.goal || 'не указана'}}` : 'короткий профиль';
    const avoidPreview = document.getElementById('foodProfileAvoidPreview');
    if (avoidPreview) avoidPreview.textContent = previewList(profile.hard_exclusions || profile.disliked_ingredients || [], 8);
    const likePreview = document.getElementById('foodProfileLikePreview');
    if (likePreview) likePreview.textContent = previewList([
        ...(profile.preferred_proteins || []),
        ...(profile.preferred_sides || []),
        ...(profile.preferred_vegetables || []),
    ], 10);
}}

function previewList(values, limit) {{
    const arr = Array.from(new Set((values || []).filter(Boolean))).slice(0, limit);
    return arr.length ? arr.join(', ') : 'пока не заполнено';
}}

function collectFoodProfile() {{
    const avoid = Array.from(document.querySelectorAll('[data-avoid]:checked')).map(el => el.dataset.avoid);
    const preferred = Array.from(document.querySelectorAll('[data-prefer]:checked')).map(el => el.dataset.prefer);
    const notes = document.getElementById('foodProfileNotes')?.value || '';
    const disliked = [];
    if (avoid.includes('legumes')) disliked.push('фасоль', 'чечевица', 'нут', 'горох');
    return {{
        avoid_groups: avoid,
        disliked_ingredients: disliked,
        preferred_proteins: preferred.filter(x => ['курица','яйца','творог','сыр','рыба'].includes(x)),
        preferred_sides: preferred.filter(x => ['рис','гречка','макароны','картофель','овсянка'].includes(x)),
        preferred_vegetables: preferred.filter(x => ['огурец','помидор','замороженные овощи'].includes(x)),
        recipe_mode: document.getElementById('recipeMode')?.checked !== false,
        max_cook_minutes: 25,
        cooking_energy: 'low',
        notes,
    }};
}}

async function saveFoodProfile() {{
    const profile = collectFoodProfile();
    if (!BRIDGE_ONLINE) {{
        localStorage.setItem('foodProfileDraft', JSON.stringify(profile));
        toast('Сохранил черновик профиля в браузере. Для записи в файл открой http://127.0.0.1:8787/.', 'ok');
        initFoodProfile();
        return;
    }}
    try {{
        const r = await fetch(apiUrl('/api/food-profile'), {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify(profile),
        }});
        const j = await r.json();
        if (!j.ok) throw new Error(j.error || 'fail');
        localStorage.removeItem('foodProfileDraft');
        Object.assign(FOOD_PROFILE, j.profile || profile);
        initFoodProfile();
        toast('Пищевой профиль сохранён', 'ok');
    }} catch (e) {{
        toast('Ошибка профиля: ' + e.message, 'err');
    }}
}}

// --- Init AI ---
renderAI('week');
renderAllCategories();
initFoodProfile();
renderMeasurements();
initServerBridge();
try {{
    const linkedTab = window.location.hash.slice(1);
    const savedTab = localStorage.getItem('health-dashboard-tab');
    const initialTab = document.getElementById('tab-' + linkedTab) ? linkedTab : savedTab || 'sleep';
    showTab(document.getElementById('tab-' + initialTab) ? initialTab : 'sleep');
}} catch (e) {{}}
// Set today's date in measurement form
{{
    const md = document.getElementById('msrDate');
    if (md) md.value = new Date().toISOString().slice(0, 10);
}}
// обновляем относительное время раз в минуту
setInterval(() => {{
    renderAI(currentPeriod);
    renderAllCategories();
    if (BRIDGE_ONLINE) refreshRuntimeStatus().catch(() => {{}});
}}, 60000);

// Esc закрывает модал
document.addEventListener('keydown', e => {{
    if (e.key === 'Escape') {{
        closeAiModal();
        closeDataHealth();
        closeWorkoutShareModal();
        const chatPanel = document.getElementById('chatPanel');
        if (chatPanel && chatPanel.classList.contains('open')) toggleChat();
    }}
}});

// --- AI Chat ---
let chatHistory = [];
try {{
    chatHistory = JSON.parse(localStorage.getItem('aiChatHistory') || '[]');
}} catch(e) {{}}
let chatBusy = false;

function saveChatHistory() {{
    localStorage.setItem('aiChatHistory', JSON.stringify(chatHistory));
}}

function clearChatHistory() {{
    if (!confirm('Очистить историю чата?')) return;
    chatHistory = [];
    saveChatHistory();
    document.getElementById('chatMessages').innerHTML = '<div class="chat-msg system">Спроси что угодно о своём здоровье — у AI есть все твои данные</div>';
}}

function toggleChat() {{
    const panel = document.getElementById('chatPanel');
    const wasOpen = panel.classList.contains('open');
    panel.classList.toggle('open');
    if (panel.classList.contains('open')) {{
        document.getElementById('chatFab').classList.remove('has-unread');
        setTimeout(() => document.getElementById('chatInput').focus(), 200);
        if (!wasOpen && chatHistory.length > 0 && document.getElementById('chatMessages').children.length <= 1) {{
            // Render history if not already rendered
            chatHistory.forEach(msg => {{
                addChatMsg(msg.role, msg.role === 'user' ? escapeHtml(msg.text) : msg.text, msg.timestamp, true);
            }});
        }}
    }}
}}

function addChatMsg(role, html, timestamp, noAnim) {{
    const box = document.getElementById('chatMessages');
    const div = document.createElement('div');
    div.className = 'chat-msg ' + role;
    if (noAnim) div.style.animation = 'none';
    
    let timeStr = '';
    if (timestamp) {{
        const d = new Date(timestamp);
        timeStr = `<div style="font-size:10px; opacity:0.5; margin-bottom:4px; text-align:${{role === 'user' ? 'right' : 'left'}}">${{d.toLocaleDateString()}} ${{d.getHours().toString().padStart(2, '0')}}:${{d.getMinutes().toString().padStart(2, '0')}}</div>`;
    }}

    if (role === 'user') {{
        div.innerHTML = timeStr + html;
    }} else {{
        div.innerHTML = timeStr + (role === 'ai' ? sanitizeAiHtml(html) : escapeHtml(String(html || '')));
    }}
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
    return div;
}}

function showTyping() {{
    const box = document.getElementById('chatMessages');
    const div = document.createElement('div');
    div.className = 'chat-typing';
    div.id = 'chatTyping';
    div.innerHTML = '<span></span><span></span><span></span>';
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
}}

function hideTyping() {{
    const el = document.getElementById('chatTyping');
    if (el) el.remove();
}}

function chatKeydown(e) {{
    if (e.key === 'Enter' && !e.shiftKey) {{
        e.preventDefault();
        sendChat();
    }}
}}

function askChip(btn) {{
    document.getElementById('chatInput').value = btn.textContent;
    sendChat();
    // Hide suggestions after first use
    document.getElementById('chatSuggestions').style.display = 'none';
}}

async function sendChat() {{
    if (chatBusy || !BRIDGE_ONLINE) {{
        if (!BRIDGE_ONLINE) toast('Сервер не запущен. Запусти dashboard_server.py', 'err');
        return;
    }}
    const input = document.getElementById('chatInput');
    const msg = input.value.trim();
    if (!msg) return;

    input.value = '';
    input.style.height = 'auto';
    const nowIso = new Date().toISOString();
    addChatMsg('user', escapeHtml(msg), nowIso);
    chatHistory.push({{ role: 'user', text: msg, timestamp: nowIso }});
    saveChatHistory();

    chatBusy = true;
    document.getElementById('chatSendBtn').disabled = true;
    document.getElementById('chatSuggestions').style.display = 'none';
    showTyping();

    try {{
        const r = await fetch(apiUrl('/api/chat'), {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{
                message: msg,
                history: chatHistory.slice(0, -1),
            }}),
        }});
        hideTyping();
        const j = await r.json();
        const aiIso = new Date().toISOString();
        if (j.ok && j.reply) {{
            addChatMsg('ai', j.reply, aiIso);
            chatHistory.push({{ role: 'ai', text: j.reply, timestamp: aiIso }});
            saveChatHistory();
        }} else {{
            addChatMsg('system', '⚠️ ' + (j.error || 'Ошибка'));
        }}
    }} catch (e) {{
        hideTyping();
        addChatMsg('system', '⚠️ Не удалось связаться с сервером');
    }} finally {{
        chatBusy = false;
        document.getElementById('chatSendBtn').disabled = false;
    }}
}}

// Auto-resize textarea
document.addEventListener('DOMContentLoaded', () => {{
    const chatInputEl = document.getElementById('chatInput');
    if (chatInputEl) {{
        chatInputEl.addEventListener('input', function() {{
            this.style.height = 'auto';
            this.style.height = Math.min(this.scrollHeight, 100) + 'px';
        }});
    }}
}});
</script>
<!-- AI Chat Widget -->
<div class="chat-panel" id="chatPanel">
    <div class="chat-header">
        <div class="chat-title"><svg class="ui-icon"><use href="#icon-sparkles"></use></svg>AI-ассистент</div>
        <div>
            <button class="chat-close" type="button" onclick="clearChatHistory()" title="Очистить историю" aria-label="Очистить историю"><svg class="ui-icon"><use href="#icon-trash"></use></svg></button>
            <button class="chat-close" type="button" onclick="toggleChat()" aria-label="Закрыть AI-ассистента"><svg class="ui-icon"><use href="#icon-close"></use></svg></button>
        </div>
    </div>
    <div class="chat-messages" id="chatMessages">
        <div class="chat-msg system">Спроси что угодно о своём здоровье — у AI есть все твои данные</div>
    </div>
    <div class="chat-suggestions" id="chatSuggestions">
        <button class="chat-chip" onclick="askChip(this)">Я похудел? Какой дефицит калорий?</button>
        <button class="chat-chip" onclick="askChip(this)">Как мой сон за неделю?</button>
        <button class="chat-chip" onclick="askChip(this)">Хватает ли мне белка?</button>
        <button class="chat-chip" onclick="askChip(this)">Что приготовить сегодня?</button>
    </div>
    <div class="chat-input-area">
        <textarea id="chatInput" placeholder="Напиши вопрос..." rows="1" onkeydown="chatKeydown(event)"></textarea>
        <button class="chat-send" id="chatSendBtn" onclick="sendChat()" title="Отправить">➤</button>
    </div>
</div>
</body>
</html>
"""


def render_html(
    include_ai: bool = False,
    server_mode: bool = False,
    demo_mode: bool = False,
) -> str:
    data = build_demo_data() if demo_mode else load_data()
    ins = compute_insights(data)
    charts = format_chart_data(data)
    ai_caches = (
        build_demo_ai_caches()
        if demo_mode
        else load_ai_caches()
        if (include_ai or server_mode)
        else {}
    )

    user = data.get("user", {})
    goals = data.get("nutrition_goals", {})
    fasting = data.get("fasting_plan") or {}
    feelings = data.get("feelings", [])
    workouts = data.get("workouts", [])
    nutr = data.get("nutrition", [])
    sleep_nights = aggregate_sleep_by_date(data.get("sleep_sessions", []))
    sleep_target_min = int(user.get("sleep_target_min") or 450)
    sleep_metrics = compute_sleep_metrics(
        data.get("sleep_sessions", []),
        target_min=sleep_target_min,
        recent_nights=7,
    )
    food_profile = build_demo_food_profile() if demo_mode else load_food_profile()
    measurements = build_demo_measurements() if demo_mode else load_measurements()

    sleep_daily = sleep_metrics.get("daily") or []
    max_sleep_debt = max((row.get("debt_min") or 0 for row in sleep_daily), default=0)
    sleep_debt_bars = "".join(
        (
            f'<span style="height:{max(4, round((row.get("debt_min") or 0) / max_sleep_debt * 100))}%;" '
            f'title="{row.get("date")}: {format_minutes_short(row.get("debt_min"))}"></span>'
        )
        for row in sleep_daily
    ) if max_sleep_debt else '<span style="height:4px" title="Долг не накоплен"></span>'
    regularity_score = sleep_metrics.get("regularity_score")
    sleep_quality_label = (
        f"{sleep_metrics.get('measured_nights', 0)}/{sleep_metrics.get('recent_nights_count', 0)} измерено"
        if sleep_metrics.get("recent_nights_count")
        else "нет данных"
    )

    wd = ins.get("weight_change_30d")
    if wd is not None:
        wd_cls = "ok" if wd < 0 else "bad" if wd > 0 else ""
        wd_label = f"{'+' if wd > 0 else ''}{wd} кг / 30д"
    else:
        wd_cls, wd_label = "", "—"

    kv = ins.get("kcal_vs_goal", 0)
    kv_cls = "bad" if kv > 150 else "warn" if kv < -200 else "ok"
    kv_label = f"{'+' if kv > 0 else ''}{kv} от цели" if goals.get("kcal") else "нет цели"

    last_meals = {}
    last_nutr_date = "—"
    if nutr:
        last_meals = nutr[0].get("meals", {})
        last_nutr_date = nutr[0]["date"]

    food_ideas = compute_food_ideas(data, profile=food_profile)
    notes_status = compute_notes_status(data)
    section_kpis = compute_section_kpis(
        data,
        insights=ins,
        sleep_metrics=sleep_metrics,
        food_profile=food_profile,
        notes_status=notes_status,
    )
    sync_notice = compute_sync_notice(data)
    notes_delta_cls = "ok" if notes_status["has_recent"] else "warn" if notes_status["total"] else "bad"
    notes_delta_label = (
        f"свежих {notes_status['recent']}"
        if notes_status["has_recent"]
        else "свежих нет"
        if notes_status["total"]
        else "пока пусто"
    )
    notes_status_meta = (
        f"Всего {notes_status['total']} · YAZIO {notes_status['yazio']} · с ПК {notes_status['manual']}"
        + (f" · последняя {notes_status['latest']}" if notes_status["latest"] else "")
    )

    fasting_plan_label = (fasting.get("plan") or "—").replace("_v2", "").replace("_", " ") if fasting else "—"
    energy_balance = compute_energy_balance(data)
    energy_today = energy_balance[-1] if energy_balance else {}
    energy_eaten = energy_today.get("eaten", "—")
    energy_goal = energy_today.get("base_goal", goals.get("kcal", "—") or "—")
    energy_raw = energy_today.get("exercise_raw", 0)
    energy_credit = energy_today.get("exercise_credit", 0)
    energy_budget = energy_today.get("adjusted_goal", energy_goal)
    remaining = energy_today.get("remaining")
    base_delta = energy_today.get("base_delta")
    adjusted_delta = energy_today.get("adjusted_delta")
    if remaining is None:
        energy_remaining_label = "—"
        energy_remaining_class = ""
    elif remaining >= 0:
        energy_remaining_label = f"+{remaining} ккал"
        energy_remaining_class = "ok" if remaining >= 150 else "warn"
    else:
        energy_remaining_label = f"{remaining} ккал"
        energy_remaining_class = "bad"
    energy_base_delta_label = (
        f"{'+' if base_delta > 0 else ''}{base_delta} ккал к базовой цели"
        if base_delta is not None else "нет данных"
    )
    energy_adjusted_delta_label = (
        f"{'+' if adjusted_delta > 0 else ''}{adjusted_delta} ккал к бюджету"
        if adjusted_delta is not None else "нет данных"
    )
    energy_note = "плюс = можно ещё, минус = перебор; активность засчитана частично"

    return HTML_TEMPLATE.format(
        display_name=user.get("name") or "Alex",
        age=user.get("age", "—"),
        height=user.get("height_cm", "—"),
        weight=user.get("weight_kg", "—"),
        weight_goal=goals.get("weight_goal_kg", "—"),
        fasting_plan=fasting_plan_label,
        synced_at=data.get("synced_at", "—").replace("T", " ")[:16],
        sync_notice_class=sync_notice["class"],
        sync_notice_text=sync_notice["text"],
        demo_mode="true" if demo_mode else "false",
        demo_banner_hidden="" if demo_mode else "hidden",
        avg_sleep_h=round(ins.get("avg_sleep_min", 0) / 60, 1) if ins.get("avg_sleep_min") else "—",
        n_sleep=len(sleep_nights),
        sleep_debt_label=format_minutes_short(sleep_metrics.get("debt_min")),
        sleep_debt_bars=sleep_debt_bars,
        sleep_target_label=format_minutes_short(sleep_metrics.get("target_min")),
        sleep_recent_nights=sleep_metrics.get("recent_nights_count", 0),
        sleep_confidence=sleep_metrics.get("confidence", "low"),
        sleep_confidence_label=sleep_quality_label,
        sleep_regularity_value=regularity_score if regularity_score is not None else "—",
        sleep_regularity_unit="/100" if regularity_score is not None else "",
        sleep_regularity_label=sleep_metrics.get("regularity_label", "мало данных"),
        sleep_bed_spread=format_minutes_short(sleep_metrics.get("bedtime_spread_min")),
        sleep_wake_spread=format_minutes_short(sleep_metrics.get("waketime_spread_min")),
        sleep_typical_bedtime=sleep_metrics.get("typical_bedtime") or "—",
        sleep_typical_waketime=sleep_metrics.get("typical_waketime") or "—",
        sleep_average_label=format_minutes_short(sleep_metrics.get("avg_sleep_min")),
        sleep_recommended_label=format_minutes_short(sleep_metrics.get("recommended_sleep_min")),
        avg_deep=ins.get("avg_deep_pct", "—"),
        avg_rem=ins.get("avg_rem_pct", "—"),
        cur_weight=ins.get("current_weight", "—"),
        weight_delta_class=wd_cls,
        weight_delta_label=wd_label,
        body_fat=ins.get("body_fat", "—"),
        muscle=ins.get("muscle", "—"),
        avg_steps=ins.get("avg_steps", "—"),
        steps_goal=goals.get("steps_goal", "—"),
        avg_kcal=ins.get("avg_kcal", "—"),
        kcal_delta_class=kv_cls,
        kcal_delta_label=kv_label,
        notes_total=notes_status["total"],
        notes_delta_class=notes_delta_cls,
        notes_delta_label=notes_delta_label,
        notes_status_message=notes_status["message"],
        notes_status_meta=notes_status_meta,
        last_nutr_date=last_nutr_date,
        energy_eaten=energy_eaten,
        energy_goal=energy_goal,
        energy_raw=energy_raw,
        energy_credit=energy_credit,
        energy_budget=energy_budget,
        energy_remaining_label=energy_remaining_label,
        energy_remaining_class=energy_remaining_class,
        energy_base_delta_label=energy_base_delta_label,
        energy_adjusted_delta_label=energy_adjusted_delta_label,
        energy_note=energy_note,
        today=datetime.now().strftime("%Y-%m-%d"),
        composer_display="block" if server_mode else "none",
        offline_display="none" if server_mode else "block",
        chart_json=json.dumps(charts, ensure_ascii=False),
        sleep_metrics_json=json.dumps(sleep_metrics, ensure_ascii=False),
        section_kpis_json=json.dumps(section_kpis, ensure_ascii=False),
        meals_json=json.dumps(last_meals, ensure_ascii=False),
        nutrition_diary_json=json.dumps(compute_nutrition_diary(data), ensure_ascii=False),
        workouts_json=json.dumps(workouts, ensure_ascii=False),
        food_ideas_json=json.dumps(food_ideas, ensure_ascii=False),
        food_profile_json=json.dumps(food_profile, ensure_ascii=False),
        feelings_json=json.dumps(feelings, ensure_ascii=False),
        ai_cache_json=json.dumps(ai_caches, ensure_ascii=False),
        measurements_json=json.dumps(measurements, ensure_ascii=False),
        measurement_fields_json=json.dumps(MEASUREMENT_FIELDS_RU, ensure_ascii=False),
        server_mode="true" if server_mode else "false",
    )


def build(
    include_ai: bool = False,
    server_mode: bool = False,
    demo_mode: bool = False,
) -> Path:
    html = render_html(
        include_ai=include_ai,
        server_mode=server_mode,
        demo_mode=demo_mode,
    )
    storage_utils.atomic_write_text(HTML_PATH, html)
    print(f"Dashboard built: {HTML_PATH} ({len(html)/1024:.0f} KB)")
    return HTML_PATH


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--open", action="store_true", help="Open in browser after build")
    parser.add_argument("--gemini", action="store_true", help="Generate/include Gemini AI for 'week'")
    parser.add_argument("--force-gemini", action="store_true", help="Force fresh Gemini (week)")
    parser.add_argument("--all-periods", action="store_true", help="Generate all Gemini periods and categories")
    parser.add_argument("--demo", action="store_true", help="Build a public dashboard with synthetic data")
    args = parser.parse_args()

    if args.gemini or args.force_gemini or args.all_periods:
        try:
            from gemini_analyzer import analyze, analyze_all
            if args.all_periods:
                analyze_all(force=args.force_gemini)
            else:
                analyze("week", force=args.force_gemini)
        except Exception as e:
            print(f"Gemini error: {e}")

    path = build(include_ai=True, server_mode=False, demo_mode=args.demo)
    if args.open:
        webbrowser.open(path.as_uri())


if __name__ == "__main__":
    main()
