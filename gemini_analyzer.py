"""
Gemini-анализатор данных здоровья через Vertex AI.

Три периода анализа:
    day   — только сегодняшний день, ситуативный, быстрый (Flash)
    week  — 7-14 дней, стандартный (Pro)
    month — 30 дней с трендами (Pro)

Авторизация: OAuth2 access token от gcloud CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import storage_utils

from build_dashboard import aggregate_sleep_by_date, load_food_profile

BASE = Path(__file__).parent
DATA_DIR = BASE / "sleep-data"
JSON_PATH = DATA_DIR / "latest_sync.json"
PROJECT_PATH = BASE / "gemini_project.txt"
MEASUREMENTS_PATH = BASE / "body_measurements.json"


def _load_body_measurements() -> list:
    """Замеры тела (грудь/талия/бедра и т.д.) — отсортированы по дате убывания."""
    if not MEASUREMENTS_PATH.exists():
        return []
    try:
        data = json.loads(MEASUREMENTS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return []
        out = []
        for date in sorted(data.keys(), reverse=True):
            entry = data[date]
            if isinstance(entry, dict):
                out.append({"date": date, **{k: v for k, v in entry.items() if k != "added_at"}})
        return out
    except Exception:
        return []

GLOBAL_PERIODS = ("day", "week", "month")
CATEGORIES = ("sleep", "body", "nutrition", "foodprofile", "activity", "health")
ALL_KEYS = GLOBAL_PERIODS + CATEGORIES

CACHE_PATHS = {
    k: (DATA_DIR / f"gemini_{k}.txt", DATA_DIR / f"gemini_{k}.meta.json")
    for k in ALL_KEYS
}

# TTL кэша в секундах. Category-анализы редкие, TTL 6ч.
CACHE_TTL = {
    "day": 1800, "week": 3600 * 3, "month": 3600 * 12,
    "sleep": 3600 * 4, "body": 3600 * 6, "nutrition": 3600 * 4, "foodprofile": 3600 * 24,
    "activity": 3600 * 4, "health": 3600 * 4,
}

GCLOUD_CANDIDATES = [
    r"C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd",
    r"C:\Program Files\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd",
    "gcloud",
]

DEFAULT_PROJECT = "sasha-ai-2026"
LOCATION = "us-central1"
GEMINI_31_PRO = "gemini-3.1-pro-preview"


def model_location(model: str) -> str:
    """Gemini 3.x preview models are served through the Vertex global endpoint."""
    return "global" if model.startswith("gemini-3") else LOCATION


def endpoint_host(location: str) -> str:
    return "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"

# Primary модель — официальный Gemini 3.1 Pro Preview. Если проекту ещё не открыт доступ,
# код сам откатится на 2.5 Pro/Flash.
DEFAULT_MODEL = {
    "day":   GEMINI_31_PRO,
    "week":  GEMINI_31_PRO,
    "month": GEMINI_31_PRO,
    "sleep":     GEMINI_31_PRO,
    "body":      GEMINI_31_PRO,
    "nutrition": GEMINI_31_PRO,
    "foodprofile": GEMINI_31_PRO,
    "activity":  GEMINI_31_PRO,
    "health":    GEMINI_31_PRO,
}

FALLBACK_MODELS = [
    GEMINI_31_PRO,
    "gemini-3-pro",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.0-flash-001",
    "gemini-1.5-pro",
]


def find_gcloud() -> str | None:
    for candidate in GCLOUD_CANDIDATES:
        p = Path(candidate)
        if p.exists():
            return str(p)
    return None


def get_access_token() -> str | None:
    gcloud = find_gcloud()
    if not gcloud:
        return None
    try:
        r = subprocess.run(
            [gcloud, "auth", "print-access-token"],
            capture_output=True, timeout=15, text=True,
        )
        if r.returncode != 0:
            return None
        return r.stdout.strip().replace("\r", "")
    except Exception:
        return None


def get_project() -> str:
    if PROJECT_PATH.exists():
        p = PROJECT_PATH.read_text(encoding="utf-8").strip()
        if p:
            return p
    env_p = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
    if env_p:
        return env_p
    gcloud = find_gcloud()
    if gcloud:
        try:
            r = subprocess.run(
                [gcloud, "config", "get-value", "project"],
                capture_output=True, timeout=10, text=True,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip().replace("\r", "")
        except Exception:
            pass
    return DEFAULT_PROJECT


def filter_by_date(items: list, date_key: str, cutoff: datetime) -> list:
    """Оставляем только записи date_key >= cutoff."""
    result = []
    for item in items:
        try:
            d_str = item.get(date_key, "")[:10]
            d = datetime.strptime(d_str, "%Y-%m-%d")
            if d >= cutoff:
                result.append(item)
        except Exception:
            continue
    return result


def _date_coverage(items: list, date_key: str) -> dict:
    dates = []
    for item in items or []:
        try:
            d_str = (item.get(date_key) or "")[:10]
            datetime.strptime(d_str, "%Y-%m-%d")
            dates.append(d_str)
        except Exception:
            continue
    return {
        "count": len(dates),
        "first_date": min(dates) if dates else None,
        "last_date": max(dates) if dates else None,
    }


def build_payload(data: dict, period: str) -> dict:
    """Формируем payload для указанного периода."""
    now = datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        cutoff = today
    elif period == "week":
        cutoff = today - timedelta(days=7)
    elif period == "month":
        cutoff = today - timedelta(days=30)
    elif period == "chat":
        cutoff = datetime(1970, 1, 1)
    else:
        # категории берут 14 дней полных данных
        cutoff = today - timedelta(days=14)

    # Для day-анализа: сегодняшний день почти всегда неполный.
    # Явно сигналим AI: текущее время и что данные частичные.
    day_progress_ratio = round((now.hour * 60 + now.minute) / (24 * 60), 2)
    context_flags = {
        "current_datetime": now.strftime("%Y-%m-%d %H:%M"),
        "current_hour": now.hour,
        "today_iso": today.strftime("%Y-%m-%d"),
        "yesterday_iso": (today - timedelta(days=1)).strftime("%Y-%m-%d"),
        "day_progress_pct": int(day_progress_ratio * 100),
        "day_is_complete": now.hour >= 23,
    }

    user = data.get("user", {})
    goals = data.get("nutrition_goals", {})
    fasting = data.get("fasting_plan")
    food_profile = load_food_profile()

    sleeps = aggregate_sleep_by_date(filter_by_date(data.get("sleep_sessions", []), "date", cutoff))
    daily = filter_by_date(data.get("daily_metrics", []), "date", cutoff)
    nutr = filter_by_date(data.get("nutrition", []), "date", cutoff)
    feelings = filter_by_date(data.get("feelings", []), "date", cutoff)
    water = filter_by_date(data.get("water_intake", []), "date", cutoff)
    workouts = filter_by_date(data.get("workouts", []), "date", cutoff)
    weights = data.get("weight_history", []) if period == "chat" else data.get("weight_history", [])[:15]

    sleep_summary = []
    for s in sleeps:
        hr = s.get("heart_rate", {}).get("avg") if s.get("heart_rate") else None
        sleep_summary.append({
            "date": s["date"], "bed": s["bedtime"], "wake": s["waketime"],
            "dur_min": s["duration_min"],
            "deep_pct": s["stages"]["deep"]["pct"],
            "rem_pct": s["stages"]["rem"]["pct"],
            "light_pct": s["stages"]["light"]["pct"],
            "awake_pct": s["stages"]["awake"]["pct"],
            "hr_avg": hr,
        })

    daily_summary = []
    for d in daily:
        act = d.get("activity", {})
        daily_summary.append({
            "date": d["date"],
            "steps": act.get("steps"),
            "kcal_activity": act.get("calories"),
            "active_min": act.get("active_min"),
            "hr_avg": d.get("hr", {}).get("avg") if d.get("hr") else None,
            "hr_min": d.get("hr", {}).get("min") if d.get("hr") else None,
            "stress_avg": d.get("stress", {}).get("avg") if d.get("stress") else None,
            "spo2_avg": d.get("spo2", {}).get("avg") if d.get("spo2") else None,
        })

    nutr_summary = []
    for n in nutr:
        item_names = []
        for meal_info in (n.get("meals") or {}).values():
            for item in (meal_info.get("items") or []):
                name = (item.get("name") or "").strip()
                if name and name not in item_names:
                    item_names.append(name)
        row = {
            "date": n["date"], "kcal": n["total_kcal"],
            "P": n["protein_g"], "F": n["fat_g"], "C": n["carb_g"],
            "fiber": n.get("fiber_g"), "sugar": n.get("sugar_g"),
        }
        if item_names:
            row["items"] = item_names[:25]
        nutr_summary.append(row)

    weight_summary = [
        {"date": w["date"], "kg": w["weight_kg"],
         "fat": w.get("body_fat_pct"), "muscle": w.get("muscle_pct")}
        for w in weights
    ]

    feelings_summary = []
    for f in feelings:
        parts = []
        if f.get("yazio_note"):
            parts.append(f"[YAZIO] {f['yazio_note']}")
        if f.get("manual_note"):
            parts.append(f"[С ПК] {f['manual_note']}")
        if f.get("note") and not parts:
            parts.append(f.get("note"))
        note = "\n".join(parts)
        tags = list(set((f.get("yazio_tags") or []) + (f.get("manual_tags") or []) + (f.get("tags") or [])))
        feelings_summary.append({
            "date": f["date"].strip('"'), "tags": tags, "note": note[:1500],
        })

    body_measurements = _load_body_measurements()

    return {
        "context": context_flags,
        "data_coverage": {
            "synced_at": data.get("synced_at"),
            "sleep": _date_coverage(data.get("sleep_sessions", []), "date"),
            "daily_metrics": _date_coverage(data.get("daily_metrics", []), "date"),
            "nutrition": _date_coverage(data.get("nutrition", []), "date"),
            "workouts": _date_coverage(data.get("workouts", []), "date"),
            "weight_history": _date_coverage(data.get("weight_history", []), "date"),
            "notes": _date_coverage(data.get("feelings", []), "date"),
        },
        "user": user, "goals": goals, "fasting": fasting,
        "food_profile": food_profile,
        "body_measurements": body_measurements,
        "sleep": sleep_summary,
        "daily_metrics": daily_summary,
        "nutrition": nutr_summary,
        "water": [{"date": w.get("date"), "ml": w.get("ml")} for w in water],
        "workouts": [
            {
                "date": w.get("date"),
                "datetime": w.get("datetime"),
                "training": w.get("training_ru") or w.get("training"),
                "duration_min": w.get("duration_min"),
                "kcal": w.get("kcal"),
                "steps": w.get("steps"),
                "distance_km": w.get("distance_km"),
                "avg_speed_kmh": w.get("avg_speed_kmh"),
                "pace_min_per_km": w.get("pace_min_per_km"),
                "hr_avg": w.get("hr_avg"),
                "hr_max": w.get("hr_max"),
                "heart_points": w.get("heart_points"),
                "trimp": w.get("trimp"),
                "zones_min": w.get("zones_min"),
                "fragment_count": w.get("fragment_count"),
                # Home Workouts breakdown (per-exercise durations) — есть только для домашних тренировок
                "exercises_count": w.get("exercises_count"),
                "exercises_avg_sec": w.get("exercises_avg_sec"),
                "exercises_breakdown": w.get("exercises_breakdown"),
                "homeworkout_type": w.get("homeworkout_type"),
                "homeworkout_day": w.get("homeworkout_day"),
                "source": w.get("source"),
                "source_app": w.get("source_app"),
            }
            for w in workouts
        ],
        "weight_history": weight_summary,
        "notes": feelings_summary,
    }


def build_prompt(data: dict, period: str) -> str:
    payload = build_payload(data, period)
    today = datetime.now().strftime("%Y-%m-%d")

    if period == "day":
        now_str = datetime.now().strftime("%H:%M")
        header = f"""Ты медицинский AI-ассистент. Сейчас {today} {now_str} — день ЕЩЁ НЕ ЗАКОНЧЕН (см. context.day_progress_pct в данных).

КРИТИЧЕСКИ ВАЖНО:
- НЕ делай итоговых выводов про "за сегодня съел мало/много калорий" — данные о питании, шагах и активности ЧАСТИЧНЫЕ, это то что успело накопиться ДО текущего момента.
- Текущие калории/шаги надо сравнивать не с целью за весь день, а с пропорциональной (цель * context.day_progress_pct / 100).
- Если сегодняшних данных мало или нет (например сон за прошлую ночь не подгрузился) — скажи об этом прямо, используй вчерашний день (context.yesterday_iso) как базу для комментариев.
- Говори в формате "на текущий момент", "пока что", "к {now_str}", а не "за день".
Дай КРАТКИЙ ситуативный анализ: как утро/день складывается, что стоит сделать до вечера, на что обратить внимание.
"""
        format_block = """
ФОРМАТ (строго HTML, без markdown):

<div class="ai-summary">Одна ёмкая фраза — как день идёт К ТЕКУЩЕМУ МОМЕНТУ (до 15 слов). Без итоговых "съел / прошёл".</div>

<h3>Как день идёт</h3>
<p>2-3 предложения о том что УЖЕ было: последняя ночь сна (если есть), активность/питание до текущего момента с поправкой на время суток. Упомяни заметки если есть.</p>

<h3>Что сделать до конца дня</h3>
<ul>
<li>Конкретное действие до вечера (с цифрами: например "добрать 1500 ккал и 80г белка до 22:00")</li>
<li>Второе действие</li>
</ul>"""
    elif period == "week":
        header = f"""Ты медицинский AI-ассистент. Проанализируй данные здоровья пользователя за ПОСЛЕДНИЕ 7 ДНЕЙ.

ВАЖНО: сегодняшний день (context.today_iso) ещё идёт — данные за него частичные (context.day_progress_pct). Не включай сегодняшний неполный день в средние и выводы, ориентируйся на ПОЛНЫЕ прошлые дни. Сегодня можно упомянуть как "начало дня" отдельно.

Полный структурированный разбор по системам. Сравнивай с нормами, приводи цифры.
"""
        format_block = """
ФОРМАТ (строго HTML, без markdown):

<div class="ai-summary">Одна ёмкая фраза — общий вывод недели.</div>

<h3>Сон</h3>
<p>Качество, режим, ЧСС во сне. С цифрами.</p>

<h3>Питание</h3>
<p>Калории, БЖУ vs цели, голодание.</p>

<h3>Активность и сердце</h3>
<p>Шаги, ЧСС покоя, стресс.</p>

<h3>Вес и состав</h3>
<p>Динамика веса и жира/мышц.</p>

<h3>Проблемы</h3>
<ul><li>Проблема 1</li><li>Проблема 2</li></ul>

<h3>Рекомендации</h3>
<ul><li>Совет 1</li><li>Совет 2</li><li>Совет 3</li></ul>"""
    elif period == "month":
        header = f"""Ты медицинский AI-ассистент. Проанализируй данные здоровья пользователя за ПОСЛЕДНИЕ 30 ДНЕЙ.

ВАЖНО: сегодняшний день (context.today_iso) ещё идёт — данные за него частичные. Исключи сегодня из средних и трендов, сравнивай только ПОЛНЫЕ дни.

Фокус на ТРЕНДАХ и ДИНАМИКЕ. Сравнивай первую половину месяца со второй. Ищи паттерны, корреляции, глобальный прогресс.
"""
        format_block = """
ФОРМАТ (строго HTML, без markdown):

<div class="ai-summary">Одна фраза — глобальный тренд месяца.</div>

<h3>Тренды</h3>
<p>Что изменилось за месяц. Прогресс или регресс. Приводи сравнения "было → стало".</p>

<h3>Корреляции</h3>
<p>Связи между сном, питанием, активностью, самочувствием. Конкретные паттерны.</p>

<h3>Ключевые достижения</h3>
<ul><li>Что получилось</li><li>...</li></ul>

<h3>Проблемные зоны</h3>
<ul><li>Что не движется или ухудшается</li><li>...</li></ul>

<h3>Стратегия на следующий месяц</h3>
<ul><li>Приоритет 1 с конкретной целью</li><li>Приоритет 2</li><li>Приоритет 3</li></ul>"""
    elif period == "sleep":
        header = f"""Ты медицинский AI-ассистент. Фокус: ТОЛЬКО СОН за последние 14 дней.

Короткий прицельный разбор. Оценка качества, режима, фаз (deep/REM), ЧСС во сне. Без общих рассуждений о здоровье — только сон.

ВАЖНО — late-night eating: проверь nutrition.meals — есть ли еда в 00-06 утра в дни с плохим сном. Если YAZIO помечает её как "snack", это всё равно late-night eating, не игнорируй. Связь: тяжелая еда перед сном бьёт по deep/REM, поднимает ЧСС во сне, провоцирует рефлюкс.

ВАЖНО — пропуски записей сна: если за какой-то ночью нет sleep_session — это НЕ значит «не спал», это значит часы не записали (не носил / разрядка). Не считай такие ночи «бессонными», явно указывай «нет данных за эту ночь — TicWatch не носил».
"""
        format_block = """
ФОРМАТ (строго HTML, без markdown):

<div class="ai-summary">Одна фраза — суть состояния сна (качество/режим/проблема).</div>

<h3>Краткий разбор</h3>
<p>3-5 предложений с конкретными цифрами: средняя длительность, % глубокого и REM vs норм (deep 13-23%, REM 20-25%), стабильность времени отхода ко сну, средняя ЧСС во сне. Отметь ухудшения/улучшения.</p>

<h3>Что делать</h3>
<ul>
<li>Конкретный совет #1 (например: "ложиться до 01:00 — у тебя средняя 03:30 и deep всего 4%")</li>
<li>Конкретный совет #2</li>
<li>Конкретный совет #3</li>
</ul>"""
    elif period == "body":
        header = f"""Ты медицинский AI-ассистент. Фокус: ТОЛЬКО СОСТАВ ТЕЛА — вес, жир, мышцы, замеры, динамика.

Короткий прицельный разбор. Без советов про сон/питание/активность — только про тело, прогресс к цели веса, анатомические выводы.

Если в данных есть body_measurements — используй их. Поля и что они означают (ВАЖНО — не путать единицы):
- chest_cm, waist_cm, hips_cm, biceps_cm, thigh_cm, calf_cm, neck_cm — ОБЪЁМЫ (обхваты), сантиметры
- shoulders_cm — ширина плеч (расстояние от точки до точки), не объём
- arm_length_cm — ДЛИНА руки от плеча до запястья (НЕ обхват бицепса)
- foot_cm — ДЛИНА стопы (размер обуви, ~27.5 см ≈ 43 размер), НЕ объём ноги

Считай только осмысленные пропорции:
- WHR = waist_cm / hips_cm для мужчин: <0.95 норма, 0.95-1.0 риск, >1.0 абдоминальное ожирение
- Талия/Рост = waist_cm / user.height_cm: <0.5 норма, >0.5 повышенный кардиориск
- Если несколько дат — сравни динамику (Δ см по каждой зоне)
- Талия — главный индикатор висцерального жира; снижение талии при стабильных бёдрах = жиросжигание

НЕ пытайся считать пропорции из foot_cm или arm_length_cm — это не обхваты. Не сравнивай их с обхватами как с ошибкой данных.

Пиши конкретные сантиметры и пропорции, не общие фразы.
"""
        format_block = """
ФОРМАТ (строго HTML, без markdown):

<div class="ai-summary">Одна фраза о прогрессе к цели веса/состава.</div>

<h3>Состав тела</h3>
<p>3-5 предложений. Текущий вес vs цель (из goals.weight_goal_kg), динамика за 30 дней, % жира и мышц, саркопения/ожирение/норма. С цифрами.</p>

<h3>Антропометрия и что менять</h3>
<ul>
<li>Конкретный вывод по телу (например: "% жира 31% — высоко для 26 лет, цель снизить до 18-22%")</li>
<li>Что даст наибольший эффект для состава (кардио/силовые/дефицит)</li>
<li>Цель на ближайший месяц в цифрах</li>
</ul>"""
    elif period == "nutrition":
        header = f"""Ты медицинский AI-ассистент. Фокус: ТОЛЬКО ПИТАНИЕ за последние 14 дней.

Короткий прицельный разбор. Калории, БЖУ vs цели, разнообразие меню, голодание, вода. Без советов про сон/тренировки — только про еду.
Для идей блюд используй поле nutrition[].items и food_profile. НЕ предлагай продукты из food_profile.avoid_groups/disliked_ingredients. НЕ предлагай случайные сладости, снеки, чипсы, орешки со сгущёнкой и похожие продукты только потому, что они "давно не встречались". Совет должен быть про полезный ближайший рацион: белок, крупа/гарнир, овощи, клетчатка, простая готовка дома.

ВАЖНО — late-night eating: пользователь часто помечает еду в 00-06 утра как "snack" (перекус) — это НЕ перекус, это late-night eating перед сном. Если в meals видишь приёмы пищи в 00-06, явно отметь это как «ел ночью / перед сном», даже если YAZIO классифицирует как snack. Связывай с качеством сна (REM падает после еды перед сном, страдает глубокий сон, усиливается рефлюкс).
"""
        format_block = """
ФОРМАТ (строго HTML, без markdown):

<div class="ai-summary">Одна фраза о состоянии питания (дефицит/профицит/баланс БЖУ/разнообразие).</div>

<h3>Краткий разбор</h3>
<p>3-5 предложений с цифрами: средние ккал vs цель, средний белок (цель 1.6-2.2 г/кг), жиры vs углеводы, клетчатка, вода, соблюдение голодания.</p>

<h3>Что поправить и что готовить</h3>
<ul>
<li>Конкретный перекос в БЖУ с фиксом ("белка только 50г при цели 150г — добавить курицу/творог/яйца")</li>
<li>2-3 идеи что приготовить в ближайшие дни: белок + гарнир + овощи. Учитывай food_profile, можно опираться на реальные продукты из items, но фильтруй нелюбимые продукты, сладости и снеки.</li>
<li>Совет по разнообразию / воде / голоданию</li>
</ul>"""
    elif period == "foodprofile":
        header = f"""Ты медицинский AI-ассистент. Фокус: ТОЛЬКО ГЛУБОКИЙ ПИЩЕВОЙ ПРОФИЛЬ пользователя.

Разбери food_profile как длинную анкету предпочтений: что реально заходит, что лучше не советовать, какие форматы еды будут устойчивыми, где риск переедания/срывов, как строить советы без давления и без нелюбимых продуктов. Не анализируй сон/активность/сердце, кроме краткой связи с заметками если это помогает контексту еды.
"""
        format_block = """
ФОРМАТ (строго HTML, без markdown):

<div class="ai-summary">Одна фраза — пищевой портрет пользователя и главный принцип рекомендаций.</div>

<h3>Портрет питания</h3>
<p>4-6 предложений: вкусы, сенсорные ограничения, любимые форматы, триггеры перекусов, готовность готовить, что нельзя предлагать. Используй конкретику из food_profile.</p>

<h3>Как давать советы</h3>
<ul>
<li>Правило #1: какие блюда/форматы советовать чаще</li>
<li>Правило #2: чего не советовать вообще или только редко</li>
<li>Правило #3: как закрывать белок/сытость без давления</li>
<li>2-3 блюда, которые подходят именно этому профилю</li>
</ul>"""
    elif period == "activity":
        header = f"""Ты медицинский AI-ассистент. Фокус: ТОЛЬКО ДВИЖЕНИЕ — шаги, тренировки, нагрузка сердца.

В данных каждой тренировки есть:
- duration_min, kcal, steps, distance_km — базовые метрики
- hr_avg, hr_max — пульс
- heart_points (Google) — 1pt/мин при ЧСС 50-70% max, 2pt/мин при ≥70%. Норма ≥150 в неделю.
- trimp (Edwards' Training Impulse) — общая нагрузка тренировки. <50 = лёгкая, 50-100 = умеренная, 100-200 = средняя, >200 = тяжёлая.
- zones_min: распределение времени по зонам пульса (z1=recovery 50-60%, z2=aerobic 60-70%, z3=tempo 70-80%, z4=threshold 80-90%, z5=VO₂max 90-100%).
- Для home_workout (Home Workouts app, программа #N): exercises_count + exercises_avg_sec + exercises_breakdown (массив [{{id, duration_s}}] по каждому упражнению с учётом пауз). Имена упражнений недоступны (id-only), но по средней длительности можно судить о характере: <30с быстрые повторы (HIIT/динамика), 30-60с стандартный круг, 60-120с длинные изометрии/планки, >120с растяжка или сессия с долгими паузами. Если HR во время тренировки нет (часы не носил) — это сигнал: советуй надевать TicWatch с режимом Exercise чтобы видеть зоны и реальную нагрузку.

Учитывай эти показатели — они дают намного больше чем просто "сколько шагов". Пользователь хочет видеть тренируется ли он эффективно или только на recovery-уровне.

Короткий прицельный разбор + КОНКРЕТНЫЕ УПРАЖНЕНИЯ НА ДОМА. Без жима лёжа и штанг — что реально можно сделать в квартире без инвентаря или с гантелями/резинками.
"""
        format_block = """
ФОРМАТ (строго HTML, без markdown):

<div class="ai-summary">Одна фраза об уровне активности (гиподинамия / норма / хорошо). Упомяни недельный Heart Points если показателен.</div>

<h3>Нагрузка и зоны</h3>
<p>3-4 предложения: средние шаги vs цель, недельный итог Heart Points (норма ≥150), сумма TRIMP за 14 дней. Какая зона доминирует в тренировках — только z1 (recovery, мало пользы для cardio fitness) или есть z2-z3 (полезный аэробный объём)? Динамика.</p>

<h3>Упражнения дома на сегодня-завтра</h3>
<ul>
<li>Упражнение #1 с подходами/повторами и группой мышц</li>
<li>Упражнение #2</li>
<li>Упражнение #3</li>
<li>Упражнение #4</li>
</ul>
<p><em>Если болен — только прогулки и лёгкая растяжка. Если zones_min показывает что почти всё время в z1 — нужно добавить интервалы для z2-z3.</em></p>"""
    elif period == "health":
        header = f"""Ты медицинский AI-ассистент. Фокус: ТОЛЬКО СЕРДЦЕ, СТРЕСС, SpO₂ за 14 дней.

Короткий прицельный разбор. ЧСС покоя, вариабельность, стресс, насыщение кислородом. Без советов по сну/еде/активности — только физиологические выводы и что делать.
"""
        format_block = """
ФОРМАТ (строго HTML, без markdown):

<div class="ai-summary">Одна фраза о сердечно-сосудистом статусе.</div>

<h3>Разбор</h3>
<p>3-5 предложений с цифрами: средний ЧСС покоя (норма 60-80, спортивный 50-65), макс ЧСС, стресс vs норма (0-50 спокойно, 50-80 напряжение, 80+ высокий), SpO₂ норма >95%. Тренды.</p>

<h3>Красные флаги и что делать</h3>
<ul>
<li>Если ЧСС покоя растёт — возможное перетренирование/болезнь/недосып</li>
<li>Если стресс высокий — рекомендации (дыхание, меньше кофеина)</li>
<li>Если SpO₂ <95% — обратиться к врачу</li>
<li>Что мониторить в ближайшее время</li>
</ul>"""
    else:
        raise ValueError(f"unknown period: {period}")

    rules = """

Используй только теги: div.ai-summary, h3, p, ul, li, strong, em. НЕ используй markdown, НЕ используй обратные кавычки. Будь КОНКРЕТЕН, с цифрами. Пиши на русском.

ОЧЕНЬ ВАЖНО — обработка симптомов из notes (трекинг трендов, не каталог жалоб):
- Симптомы из заметок 5+ дней назад НЕ могут считаться текущими, если в свежих заметках они не упоминаются.
- Если по одному симптому есть несколько заметок (например кашель упоминался 18, 20, 23 апреля), смотри на ПОСЛЕДНЮЮ. Если в ней «прошёл/лучше/реже» — состояние разрешилось.
- НЕ переноси старые симптомы в выводы про текущее состояние. Можно упомянуть как контекст («после перенесённой недавно простуды»), но не как актуальную проблему.
- Пример правильно: пользователь 18-22 апр писал про кашель, 23 апр «кашель меньше», 24 апр «кашель прошёл». В отчёте за 1 мая упомянуть «недавно перенёс простуду» — норм, говорить «продолжается кашель» — НЕТ.
- Острые жалобы (сердце, спазмы, головная боль) — упоминай только если в свежих заметках (последние 2-3 дня) или если паттерн повторяется регулярно."""

    return header + f"\nДАННЫЕ:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n" + format_block + rules


def call_vertex(token: str, project: str, model: str, prompt: str, max_tokens: int = 8000) -> str | None:
    location = model_location(model)
    host = endpoint_host(location)
    url = (
        f"https://{host}/v1/projects/{project}"
        f"/locations/{location}/publishers/google/models/{model}:generateContent"
    )
    generation_config = {"maxOutputTokens": max_tokens}
    if model.startswith("gemini-3"):
        generation_config["thinkingConfig"] = {"thinkingLevel": "MEDIUM"}
    else:
        generation_config["temperature"] = 0.5
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": generation_config,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    })
    print(f"    endpoint: {location}")
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read())
    cands = result.get("candidates", [])
    if not cands:
        return None
    parts = cands[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    usage = result.get("usageMetadata", {})
    print(f"    tokens: total={usage.get('totalTokenCount')} "
          f"think={usage.get('thoughtsTokenCount', 0)} out={usage.get('candidatesTokenCount', 0)}")
    return text if text else None


def load_cache(period: str) -> tuple[str | None, dict | None]:
    txt_path, meta_path = CACHE_PATHS[period]
    if txt_path.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return txt_path.read_text(encoding="utf-8"), meta
        except Exception:
            pass
    return None, None


def save_cache(period: str, text: str, model: str, project: str):
    txt_path, meta_path = CACHE_PATHS[period]
    now = datetime.now()
    storage_utils.atomic_write_text(txt_path, text)
    storage_utils.atomic_write_json(meta_path, {
        "timestamp": now.timestamp(),
        "iso": now.isoformat(timespec="seconds"),
        "period": period,
        "model": model,
        "project": project,
        "chars": len(text),
    })


def analyze(period: str = "week", force: bool = False) -> str:
    if period not in CACHE_PATHS:
        raise ValueError(f"period must be one of {list(CACHE_PATHS)}")

    if not JSON_PATH.exists():
        raise RuntimeError(f"Нет {JSON_PATH}. Запустите health_sync.py сначала.")

    if not force:
        cached, meta = load_cache(period)
        if cached and meta:
            age = datetime.now().timestamp() - meta.get("timestamp", 0)
            if age < CACHE_TTL[period]:
                print(f"[{period}] cache hit ({int(age)}s old, model={meta.get('model')})")
                return cached

    token = get_access_token()
    if not token:
        raise RuntimeError("Не удалось получить OAuth через gcloud. Выполните: gcloud auth login")
    project = get_project()

    with open(JSON_PATH, encoding="utf-8") as f:
        data = json.load(f)
    prompt = build_prompt(data, period)
    print(f"[{period}] prompt {len(prompt)} chars, project={project}")

    models_order = [DEFAULT_MODEL[period]] + [m for m in FALLBACK_MODELS if m != DEFAULT_MODEL[period]]
    max_tokens = 4000 if period == "day" else 8000

    errors = []
    for model in models_order:
        try:
            print(f"[{period}] trying {model}...")
            text = call_vertex(token, project, model, prompt, max_tokens)
            if text:
                save_cache(period, text, model, project)
                print(f"[{period}] OK: {len(text)} chars via {model}")
                return text
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:200]
            errors.append(f"{model}: HTTP {e.code} {body}")
            print(f"    {model}: HTTP {e.code}")
        except Exception as e:
            errors.append(f"{model}: {e}")
            print(f"    {model}: {e}")

    raise RuntimeError(f"[{period}] не удалось:\n" + "\n".join(errors[:3]))


SYMPTOM_KEYWORDS = {
    "голова": ["голова", "головн", "мигрен", "висок", "лоб", "затылк"],
    "сердце": ["сердце", "сердечн", "груд", "давит", "колет", "прихват", "стуч", "тахикард"],
    "живот": ["живот", "желуд", "тошнот", "спазм", "пищевар", "понос", "запор", "вздут", "изжог"],
    "давление": ["давлен", "гиперт", "гипот"],
    "тревога": ["тревог", "паник", "беспоко", "стресс", "нервн", "руминац"],
    "усталость": ["устал", "сонлив", "разбит", "вял", "слаб", "энерги", "сил нет", "истощ"],
    "дыхание": ["дыхан", "одышк", "задых", "не хват"],
    "кашель": ["кашел", "кашл", "горл", "першит"],
    "насморк": ["насморк", "нос залож", "сопл"],
    "температура": ["температур", "лихорад", "жар"],
    "боль": ["болит", "ноет", "ломит", "болн"],
    "тошнота": ["тошн", "рвот", "мутит"],
    "мышцы": ["мышц", "судорог", "крепатур"],
    "суставы": ["сустав", "колен", "локт", "плеч"],
    "спина": ["спин", "поясниц", "позвоноч"],
}


def _classify_symptom(symptom: str) -> list[str]:
    """Возвращает категории симптомов для поиска похожих эпизодов."""
    text = symptom.lower()
    matched = []
    for cat, kws in SYMPTOM_KEYWORDS.items():
        if any(kw in text for kw in kws):
            matched.append(cat)
    return matched or ["боль"]  # дефолт — общий поиск по болевым словам


def _find_past_episodes(symptom: str, days_back: int = 365) -> list[dict]:
    """Ищет в manual_notes.json и yazio_notes_archive.json упоминания похожих симптомов."""
    cats = _classify_symptom(symptom)
    all_keywords = set()
    for cat in cats:
        all_keywords.update(SYMPTOM_KEYWORDS.get(cat, []))
    if not all_keywords:
        all_keywords = {w.lower() for w in re.findall(r"\w{4,}", symptom)}

    cutoff = datetime.now() - timedelta(days=days_back)
    today_iso = datetime.now().strftime("%Y-%m-%d")

    episodes: list[dict] = []

    manual_path = BASE / "manual_notes.json"
    if manual_path.exists():
        try:
            notes = json.loads(manual_path.read_text(encoding="utf-8"))
            for date, entries in notes.items():
                if date == today_iso:
                    continue
                try:
                    if datetime.strptime(date, "%Y-%m-%d") < cutoff:
                        continue
                except Exception:
                    continue
                for e in entries or []:
                    txt = (e.get("text") or "").lower()
                    if any(kw in txt for kw in all_keywords):
                        episodes.append({
                            "date": date,
                            "time": e.get("time"),
                            "source": "manual",
                            "text": e.get("text"),
                            "tags": e.get("tags") or [],
                        })
        except Exception:
            pass

    archive_path = BASE / "yazio_notes_archive.json"
    if archive_path.exists():
        try:
            arch = json.loads(archive_path.read_text(encoding="utf-8"))
            if isinstance(arch, dict):
                for date, item in arch.items():
                    if date == today_iso:
                        continue
                    try:
                        if datetime.strptime(date, "%Y-%m-%d") < cutoff:
                            continue
                    except Exception:
                        continue
                    note = ""
                    tags = []
                    if isinstance(item, dict):
                        note = item.get("note") or ""
                        tags = item.get("tags") or []
                    elif isinstance(item, list):
                        for sub in item:
                            if isinstance(sub, dict):
                                note += " " + (sub.get("note") or "")
                                tags.extend(sub.get("tags") or [])
                    txt = note.lower()
                    if any(kw in txt for kw in all_keywords):
                        episodes.append({
                            "date": date,
                            "source": "yazio",
                            "text": note.strip()[:600],
                            "tags": tags,
                        })
        except Exception:
            pass

    episodes.sort(key=lambda x: x.get("date", ""), reverse=True)
    return episodes[:12]


def _build_symptom_prompt(symptom: str, payload: dict, past_episodes: list[dict]) -> str:
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""Ты медицинский AI-ассистент. Пользователь СЕЙЧАС жалуется: «{symptom}»
Сейчас {today}.

ЗАДАЧА: дать топ-3 вероятные причины ИМЕННО ЭТОГО эпизода через pattern-matching по данным.

ВАЖНО:
- Это НЕ диагноз. Это поиск паттернов в собственных данных пользователя.
- Опирайся на ДАННЫЕ ЗА 7 ДНЕЙ + ПОХОЖИЕ ЭПИЗОДЫ. Конкретные цифры обязательны.
- Ищи свежие отклонения (1-3 дня): сон, алкоголь, кофе, недоедание/переедание, нагрузка, ЧСС покоя, стресс из заметок.
- Если есть ПОХОЖИЕ ЭПИЗОДЫ в прошлом — найди что было общим тогда (это сильный сигнал).
- Если данных мало — скажи об этом честно, не выдумывай корреляции.
- Ранжируй по убыванию вероятности, дай примерные % (сумма ≈100%).

ФОРМАТ (строго HTML, без markdown, без обратных кавычек):

<div class="ai-summary">Одна фраза — наиболее вероятная причина с ключевой цифрой.</div>

<h3>Топ-3 вероятные причины</h3>
<ol>
<li><strong>Причина 1 (~XX%)</strong> — краткое обоснование. Конкретные цифры/факты из данных за последние дни. Если связано с прошлым эпизодом — упомянуть.</li>
<li><strong>Причина 2 (~YY%)</strong> — аналогично.</li>
<li><strong>Причина 3 (~ZZ%)</strong> — аналогично.</li>
</ol>

<h3>Похожие эпизоды</h3>
<p>Если в past_episodes есть совпадения — перечисли 2-3 даты и общий паттерн (например: «после ночей &lt;5ч сна» или «всегда после алкоголя»). Если нет похожих — напиши «первое зафиксированное упоминание» и укажи что мониторить чтобы понять триггер в следующий раз.</p>

<h3>Что сделать сейчас</h3>
<ul>
<li>Конкретное действие (вода / отдых / измерить пульс/давление если есть тонометр / поесть и т.п.)</li>
<li>Что мониторить ближайшие 1-2 часа</li>
<li>Красный флаг — когда обращаться к врачу: конкретный признак (например «если SpO₂ опустится ниже 94», «если боль не проходит за час», «если онемение в руке»)</li>
</ul>

ДАННЫЕ ЗА 7 ДНЕЙ (включая сегодня):
{json.dumps(payload, ensure_ascii=False, indent=2)}

ПОХОЖИЕ ЭПИЗОДЫ В ПРОШЛОМ ({len(past_episodes)}):
{json.dumps(past_episodes, ensure_ascii=False, indent=2)}
"""


def analyze_symptom(symptom: str) -> dict:
    """Острый разбор симптома: топ-3 причины + похожие эпизоды.

    symptom — короткий текст ('сердце прихватило', 'болит голова', 'устал', 'тревога').
    Возвращает dict с text (HTML), past_episodes_count, model, iso.
    """
    symptom = (symptom or "").strip()
    if not symptom:
        raise ValueError("symptom не задан")

    if not JSON_PATH.exists():
        raise RuntimeError(f"Нет {JSON_PATH}. Запустите health_sync.py.")

    token = get_access_token()
    if not token:
        raise RuntimeError("Не удалось получить OAuth через gcloud. Выполните: gcloud auth login")
    project = get_project()

    with open(JSON_PATH, encoding="utf-8") as f:
        data = json.load(f)
    payload = build_payload(data, "week")

    past_episodes = _find_past_episodes(symptom, days_back=365)
    prompt = _build_symptom_prompt(symptom, payload, past_episodes)
    print(f"[symptom='{symptom}'] prompt {len(prompt)} chars, past_episodes={len(past_episodes)}")

    models_order = [GEMINI_31_PRO] + [m for m in FALLBACK_MODELS if m != GEMINI_31_PRO]
    errors = []
    for model in models_order:
        try:
            text = call_vertex(token, project, model, prompt, max_tokens=4000)
            if text:
                return {
                    "ok": True,
                    "symptom": symptom,
                    "text": text,
                    "past_episodes_count": len(past_episodes),
                    "categories": _classify_symptom(symptom),
                    "model": model,
                    "iso": datetime.now().isoformat(timespec="seconds"),
                }
        except urllib.error.HTTPError as e:
            errors.append(f"{model}: HTTP {e.code}")
        except Exception as e:
            errors.append(f"{model}: {e}")

    raise RuntimeError("Gemini недоступен: " + "; ".join(errors[:3]))


def analyze_all(force: bool = False, keys: tuple = ALL_KEYS) -> dict:
    result = {}
    for period in keys:
        try:
            result[period] = analyze(period, force=force)
        except Exception as e:
            print(f"[{period}] FAIL: {e}", file=sys.stderr)
            result[period] = None
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--period",
        choices=list(ALL_KEYS) + ["all", "globals", "categories"],
        default="week",
        help="one of: day/week/month/sleep/body/nutrition/foodprofile/activity/health, or all/globals/categories",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    try:
        if args.period == "all":
            analyze_all(force=args.force)
        elif args.period == "globals":
            analyze_all(force=args.force, keys=GLOBAL_PERIODS)
        elif args.period == "categories":
            analyze_all(force=args.force, keys=CATEGORIES)
        else:
            text = analyze(args.period, force=args.force)
            print("\n" + text)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
