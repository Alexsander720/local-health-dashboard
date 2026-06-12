from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta


def _iso(day: date) -> str:
    return day.isoformat()


def build_demo_data(*, anchor_date: date | None = None) -> dict:
    anchor = anchor_date or date.today()
    daily_metrics = []
    weight_history = []
    nutrition = []
    sleep_sessions = []

    for offset in range(44, -1, -1):
        day = anchor - timedelta(days=offset)
        index = 44 - offset
        steps = round(6300 + 2300 * math.sin(index / 4.2) + (index % 5) * 260)
        active_min = round(34 + 17 * math.sin(index / 5.1) + (index % 3) * 4, 1)
        distance_m = round(max(0, steps * 0.72), 1)
        daily_metrics.append(
            {
                "date": _iso(day),
                "hr": {"count": 850, "min": 49, "max": 134, "avg": round(67 - index * 0.06 + math.sin(index / 3))},
                "stress": {"count": 42, "min": 18, "max": 68, "avg": round(38 - index * 0.08 + math.sin(index / 4) * 4)},
                "spo2": {"count": 380, "min": 94, "max": 100, "avg": round(97.1 + math.sin(index / 6) * 0.4, 1)},
                "activity": {
                    "steps": max(1800, steps),
                    "distance_m": distance_m,
                    "calories": round(360 + active_min * 7.4, 1),
                    "active_min": max(8, active_min),
                },
            }
        )

        weight = round(86.8 - index * 0.055 + math.sin(index / 5) * 0.25, 1)
        fat = round(27.4 - index * 0.045 + math.sin(index / 7) * 0.18, 1)
        weight_history.append(
            {
                "date": _iso(day),
                "time": "08:10",
                "weight_kg": weight,
                "bmi": round(weight / (1.72**2), 1),
                "body_fat_pct": fat,
                "muscle_pct": round(67.1 + index * 0.035, 1),
                "moisture_pct": round(51.2 + index * 0.02, 1),
                "basal_metabolism": round(1740 - index * 1.2),
                "visceral_fat": round(10.0 - index * 0.025, 1),
                "bone_mass_kg": 3.1,
                "body_score": min(88, 68 + index // 3),
            }
        )

    for offset in range(20, -1, -1):
        day = anchor - timedelta(days=offset)
        index = 20 - offset
        total = round(1940 + math.sin(index / 2.1) * 190)
        protein = round(126 + math.sin(index / 3) * 12, 1)
        fat = round(68 + math.cos(index / 4) * 8, 1)
        carbs = round(max(120, (total - protein * 4 - fat * 9) / 4), 1)
        nutrition.append(
            {
                "date": _iso(day),
                "total_kcal": total,
                "protein_g": protein,
                "fat_g": fat,
                "carb_g": carbs,
                "sugar_g": round(34 + math.sin(index) * 6, 1),
                "fiber_g": round(25 + math.cos(index / 2) * 3, 1),
                "meals": {
                    "breakfast": {
                        "kcal": 480,
                        "protein_g": 27,
                        "fat_g": 14,
                        "carb_g": 58,
                        "items": [
                            {"name": "Овсянка с ягодами", "amount_g": 280, "kcal": 330},
                            {"name": "Йогурт", "amount_g": 180, "kcal": 150},
                        ],
                    },
                    "lunch": {
                        "kcal": 690,
                        "protein_g": 49,
                        "fat_g": 22,
                        "carb_g": 72,
                        "items": [
                            {"name": "Курица с киноа и овощами", "amount_g": 420, "kcal": 690},
                        ],
                    },
                    "dinner": {
                        "kcal": max(420, total - 1170),
                        "protein_g": 42,
                        "fat_g": 24,
                        "carb_g": 48,
                        "items": [
                            {"name": "Лосось, картофель и салат", "amount_g": 390, "kcal": max(420, total - 1170)},
                        ],
                    },
                },
            }
        )

    for offset in range(13, -1, -1):
        day = anchor - timedelta(days=offset)
        index = 13 - offset
        bedtime_minutes = 23 * 60 + 18 + round(math.sin(index / 2) * 19)
        sleep_minutes = round(420 + index * 2.6 + math.sin(index / 1.7) * 24)
        awake = round(18 + abs(math.sin(index)) * 10, 1)
        duration = sleep_minutes + awake
        deep = round(sleep_minutes * (0.17 + math.sin(index / 3) * 0.012), 1)
        rem = round(sleep_minutes * (0.22 + math.cos(index / 4) * 0.012), 1)
        light = round(sleep_minutes - deep - rem, 1)
        start = datetime.combine(day, time()) + timedelta(minutes=bedtime_minutes)
        if bedtime_minutes >= 24 * 60:
            start += timedelta(days=1)
        end = start + timedelta(minutes=duration)
        sleep_sessions.append(
            {
                "date": _iso(day),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "bedtime": f"{(bedtime_minutes // 60) % 24:02d}:{bedtime_minutes % 60:02d}",
                "waketime": end.strftime("%H:%M"),
                "duration_min": duration,
                "sleep_min": sleep_minutes,
                "stages": {
                    "deep": {"min": deep, "pct": round(deep / duration * 100, 1)},
                    "rem": {"min": rem, "pct": round(rem / duration * 100, 1)},
                    "light": {"min": light, "pct": round(light / duration * 100, 1)},
                    "awake": {"min": awake, "pct": round(awake / duration * 100, 1)},
                },
                "heart_rate": {"count": round(duration), "min": 48, "max": 82, "avg": round(61 - index * 0.08)},
                "spo2": {"count": round(duration * 0.9), "min": 94, "max": 100, "avg": 97},
                "stress": {"count": 12, "min": 16, "max": 48, "avg": 28},
            }
        )

    workouts = []
    workout_types = [
        ("walking", "Быстрая ходьба", 44, 310, 5.2),
        ("strength_training", "Силовая тренировка", 58, 420, 0),
        ("cycling", "Велосипед", 72, 610, 21.4),
        ("yoga", "Йога", 36, 170, 0),
        ("walking", "Вечерняя прогулка", 51, 340, 5.9),
        ("strength_training", "Силовая тренировка", 61, 445, 0),
    ]
    for index, (kind, title, duration, kcal, distance) in enumerate(workout_types):
        day = anchor - timedelta(days=index * 3 + 1)
        workouts.append(
            {
                "id": f"demo-{index + 1}",
                "datetime": f"{_iso(day)} 18:{10 + index:02d}:00",
                "date": _iso(day),
                "training": kind,
                "training_ru": title,
                "duration_min": duration,
                "kcal": kcal,
                "distance_km": distance,
                "hr_avg": 108 + index * 3,
                "hr_max": 138 + index * 4,
                "heart_points": 28 + index * 6,
                "trimp": 36 + index * 8,
                "source": "Demo",
                "source_app": "Synthetic Health",
            }
        )

    feelings = [
        {
            "date": _iso(anchor - timedelta(days=1)),
            "yazio_note": None,
            "yazio_tags": [],
            "manual_note": "Энергии больше после стабильного сна и прогулки.",
            "manual_tags": ["сон", "энергия"],
        },
        {
            "date": _iso(anchor - timedelta(days=4)),
            "yazio_note": None,
            "yazio_tags": [],
            "manual_note": "Силовая прошла легче, чем на прошлой неделе.",
            "manual_tags": ["тренировка"],
        },
        {
            "date": _iso(anchor - timedelta(days=8)),
            "yazio_note": None,
            "yazio_tags": [],
            "manual_note": "Поздний ужин немного ухудшил засыпание.",
            "manual_tags": ["питание", "сон"],
        },
    ]

    weight_history.reverse()
    nutrition.reverse()
    sleep_sessions.reverse()
    return {
        "synced_at": f"{_iso(anchor)}T09:30:00",
        "user": {
            "name": "Demo User",
            "height_cm": 172,
            "weight_kg": weight_history[0]["weight_kg"],
            "age": 32,
            "gender": "female",
            "sleep_target_min": 450,
        },
        "nutrition_goals": {
            "kcal": 2050,
            "protein_g": 130,
            "fat_g": 70,
            "carb_g": 245,
            "steps_goal": 9000,
            "weight_goal_kg": 78,
        },
        "fasting_plan": {"plan": "12_12"},
        "sleep_sessions": sleep_sessions,
        "daily_metrics": daily_metrics,
        "weight_history": weight_history,
        "nutrition": nutrition,
        "feelings": feelings,
        "workouts": workouts,
        "sync_status": {
            "demo": True,
            "phone_online": False,
            "used_cached_dbs": False,
            "phone_base": None,
        },
    }


def build_demo_measurements(*, anchor_date: date | None = None) -> dict:
    anchor = anchor_date or date.today()
    return {
        _iso(anchor - timedelta(days=42)): {
            "chest_cm": 101,
            "shoulders_cm": 44,
            "waist_cm": 91,
            "hips_cm": 106,
            "biceps_cm": 31,
            "thigh_cm": 61,
            "calf_cm": 39,
            "added_at": f"{_iso(anchor - timedelta(days=42))}T08:15:00",
        },
        _iso(anchor): {
            "chest_cm": 99,
            "shoulders_cm": 44,
            "waist_cm": 87,
            "hips_cm": 104,
            "biceps_cm": 31.5,
            "thigh_cm": 60,
            "calf_cm": 38.5,
            "added_at": f"{_iso(anchor)}T08:20:00",
        },
    }


def build_demo_food_profile() -> dict:
    return {
        "profile_version": 2,
        "updated_at": None,
        "recipe_mode": True,
        "max_cook_minutes": 30,
        "cooking_energy": "medium",
        "goal": "Постепенное снижение веса",
        "priorities": ["Белок", "Клетчатка", "Простые блюда"],
        "hard_exclusions": [],
        "soft_dislikes": ["слишком острое"],
        "rare_ok": ["десерты"],
        "untested": [],
        "liked_ingredients": ["лосось", "курица", "овсянка", "ягоды", "авокадо"],
        "avoid_groups": [],
        "disliked_ingredients": [],
        "preferred_proteins": ["курица", "рыба", "яйца", "творог"],
        "preferred_sides": ["рис", "киноа", "овсянка", "картофель"],
        "preferred_vegetables": ["огурец", "помидор", "брокколи", "зелень"],
        "preferred_fruits": ["банан", "яблоко", "ягоды"],
        "preferred_dishes": ["боулы", "салаты", "запечённая рыба"],
        "comfort_formats": ["боул", "тарелка"],
        "satiety_foods": ["картофель", "овсянка", "творог"],
        "snack_triggers": [],
        "real_life_treats": ["тёмный шоколад"],
        "safe_foods": ["йогурт", "банан", "орехи"],
        "kitchen_equipment": ["духовка", "сковорода"],
        "survey_answers": {"demo": "synthetic"},
        "notes": "Синтетический профиль для публичной демонстрации.",
        "source": "synthetic-demo",
        "source_files": [],
        "source_markdown_file": None,
    }


def build_demo_ai_caches(*, anchor_date: date | None = None) -> dict:
    anchor = anchor_date or date.today()
    iso = f"{_iso(anchor)}T09:30:00"
    summaries = {
        "day": "Сегодня хороший баланс нагрузки и восстановления; главный фокус — сохранить время отхода ко сну.",
        "week": "Сон стал регулярнее, активность выросла, а вес плавно снижается без резких ограничений.",
        "month": "За месяц улучшились талия, регулярность сна и среднее число шагов.",
        "sleep": "Продолжительность сна близка к цели, а разброс времени засыпания постепенно уменьшается.",
        "body": "Вес и талия снижаются плавно, при этом доля мышечной массы сохраняется.",
        "nutrition": "Белок близок к цели; клетчатка и распределение калорий по дню выглядят устойчиво.",
        "foodprofile": "Рекомендации учитывают предпочтение простых белковых блюд и ограничение времени готовки.",
        "activity": "Ходьба и две силовые тренировки создают сбалансированную недельную нагрузку.",
        "health": "Пульс, стресс и SpO₂ стабильны в пределах доступных данных.",
    }
    return {
        period: {
            "text": f'<div class="ai-summary">{summary}</div><p>{summary}</p>',
            "iso": iso,
            "model": "demo-insight",
        }
        for period, summary in summaries.items()
    }
