from __future__ import annotations

from datetime import date


def _number(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _rows(data: dict, key: str) -> list[dict]:
    return sorted(
        (row for row in data.get(key, []) or [] if isinstance(row, dict)),
        key=lambda row: row.get("date") or "",
        reverse=True,
    )


def _minutes_label(value) -> str:
    minutes = max(0, round(_number(value)))
    if not minutes:
        return "—"
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours} ч {rest} мин"
    if hours:
        return f"{hours} ч"
    return f"{rest} мин"


def _integer_label(value) -> str:
    if value is None:
        return "—"
    return f"{round(_number(value)):,}".replace(",", " ")


def _weight_delta(rows: list[dict]) -> float | None:
    points = [
        (row.get("date"), _number(row.get("weight_kg"), default=-1))
        for row in rows
        if row.get("date") and _number(row.get("weight_kg"), default=-1) > 0
    ]
    if len(points) < 2:
        return None
    latest_date, latest_weight = points[0]
    try:
        latest_day = date.fromisoformat(latest_date)
    except ValueError:
        return round(latest_weight - points[-1][1], 1)

    comparison = points[-1]
    for point in points[1:]:
        try:
            if (latest_day - date.fromisoformat(point[0])).days >= 28:
                comparison = point
                break
        except ValueError:
            continue
    return round(latest_weight - comparison[1], 1)


def _average(values: list[float]) -> float | None:
    clean = [value for value in values if value > 0]
    return sum(clean) / len(clean) if clean else None


def compute_overview(data: dict, sleep_metrics: dict) -> dict:
    """Build a deterministic cross-domain summary without diagnostic claims."""
    goals = data.get("nutrition_goals", {}) or {}
    daily = _rows(data, "daily_metrics")
    nutrition = _rows(data, "nutrition")
    weights = _rows(data, "weight_history")

    latest_activity = (daily[0].get("activity") or {}) if daily else {}
    latest_nutrition = nutrition[0] if nutrition else {}
    latest_sleep = round(_number(sleep_metrics.get("latest_sleep_min")))
    average_sleep = round(_number(sleep_metrics.get("avg_sleep_min")))
    target_sleep = round(_number(sleep_metrics.get("target_min"), 450))
    sleep_debt = round(_number(sleep_metrics.get("debt_min")))
    latest_steps = round(_number(latest_activity.get("steps")))
    steps_goal = round(_number(goals.get("steps_goal"), 8000))
    latest_protein = round(_number(latest_nutrition.get("protein_g")))
    protein_goal = round(_number(goals.get("protein_g")))

    actions = []
    if latest_sleep and (latest_sleep < target_sleep - 45 or sleep_debt >= 120):
        actions.append(
            {
                "key": "sleep",
                "title": "Защити окно сна",
                "detail": (
                    f"Последняя ночь — {_minutes_label(latest_sleep)}. "
                    f"Ориентир — {_minutes_label(target_sleep)}; добавляй время постепенно."
                ),
                "tone": "priority",
                "icon": "sleep",
            }
        )
    if latest_steps and steps_goal and latest_steps < steps_goal * 0.8:
        missing_steps = max(0, steps_goal - latest_steps)
        actions.append(
            {
                "key": "movement",
                "title": "Добери спокойное движение",
                "detail": (
                    f"До ориентира не хватает около {_integer_label(missing_steps)} шагов. "
                    "Раздели их на две короткие прогулки."
                ),
                "tone": "attention",
                "icon": "steps",
            }
        )
    if latest_protein and protein_goal and latest_protein < protein_goal * 0.8:
        missing_protein = max(0, protein_goal - latest_protein)
        actions.append(
            {
                "key": "protein",
                "title": "Добавь белок в следующий приём пищи",
                "detail": (
                    f"Сейчас {_integer_label(latest_protein)} из {_integer_label(protein_goal)} г. "
                    f"Осталось примерно {_integer_label(missing_protein)} г до ориентира."
                ),
                "tone": "attention",
                "icon": "nutrition",
            }
        )

    if not actions:
        actions.append(
            {
                "key": "maintain",
                "title": "Сохрани текущий ритм",
                "detail": "Сон, движение и белок сейчас близки к заданным ориентирам.",
                "tone": "steady",
                "icon": "target",
            }
        )

    primary = actions[0]["key"]
    if primary == "sleep":
        headline = "Сегодня приоритет — восстановление"
    elif primary == "movement":
        headline = "Сегодня приоритет — немного больше движения"
    elif primary == "protein":
        headline = "Сегодня приоритет — поддержать питание"
    else:
        headline = "Показатели устойчивы — держи курс"

    if len(actions) == 1 and primary == "maintain":
        summary = "Ключевые ориентиры выполнены. Не усложняй день: повтори рабочий режим."
    elif len(actions) == 1:
        summary = (
            "Найден 1 практический шаг на основе последних записей. "
            "Сфокусируйся на нём, не пытаясь менять всё сразу."
        )
    else:
        summary = (
            f"Найдено {len(actions)} практических шага на основе последних записей. "
            "Сначала восстановление, затем движение и питание."
        )

    sleep_ratio = latest_sleep / target_sleep if target_sleep and latest_sleep else None
    steps_ratio = latest_steps / steps_goal if steps_goal and latest_steps else None
    protein_ratio = latest_protein / protein_goal if protein_goal and latest_protein else None
    signals = [
        {
            "key": "sleep",
            "label": "Последний сон",
            "value": _minutes_label(latest_sleep),
            "detail": f"ориентир {_minutes_label(target_sleep)}",
            "tone": "good" if sleep_ratio and sleep_ratio >= 0.9 else "low",
            "icon": "sleep",
            "progress": round(min(100, (sleep_ratio or 0) * 100)),
        },
        {
            "key": "steps",
            "label": "Шаги за день",
            "value": _integer_label(latest_steps) if latest_steps else "—",
            "detail": f"ориентир {_integer_label(steps_goal)}",
            "tone": "good" if steps_ratio and steps_ratio >= 0.8 else "low",
            "icon": "steps",
            "progress": round(min(100, (steps_ratio or 0) * 100)),
        },
        {
            "key": "protein",
            "label": "Белок",
            "value": f"{_integer_label(latest_protein)} г" if latest_protein else "—",
            "detail": f"ориентир {_integer_label(protein_goal)} г" if protein_goal else "цель не задана",
            "tone": "good" if protein_ratio and protein_ratio >= 0.8 else "low",
            "icon": "nutrition",
            "progress": round(min(100, (protein_ratio or 0) * 100)),
        },
    ]

    recent_steps = [
        _number((row.get("activity") or {}).get("steps"))
        for row in daily[:7]
    ]
    previous_steps = [
        _number((row.get("activity") or {}).get("steps"))
        for row in daily[7:14]
    ]
    average_steps = _average(recent_steps)
    previous_average_steps = _average(previous_steps)
    step_delta = (
        round((average_steps - previous_average_steps) / previous_average_steps * 100)
        if average_steps and previous_average_steps
        else None
    )
    weight_delta = _weight_delta(weights)

    if average_sleep:
        sleep_difference = average_sleep - target_sleep
        sleep_detail = (
            f"{abs(round(sleep_difference))} мин {'выше' if sleep_difference >= 0 else 'ниже'} ориентира"
        )
    else:
        sleep_detail = "недостаточно записей"
    if weight_delta is None:
        weight_value = "—"
        weight_detail = "нужно хотя бы два измерения"
        weight_tone = "neutral"
    else:
        weight_value = f"{weight_delta:+.1f} кг"
        weight_detail = "изменение примерно за 30 дней"
        weight_tone = "good" if weight_delta < 0 else "attention" if weight_delta > 0 else "neutral"
    if step_delta is None:
        steps_detail = "сравнение появится после 14 дней"
    else:
        steps_detail = f"{step_delta:+d}% к предыдущим 7 дням"

    trends = [
        {
            "key": "sleep",
            "label": "Средний сон",
            "value": _minutes_label(average_sleep),
            "detail": sleep_detail,
            "tone": "good" if average_sleep and average_sleep >= target_sleep - 30 else "attention",
            "icon": "sleep",
        },
        {
            "key": "weight",
            "label": "Вес",
            "value": weight_value,
            "detail": weight_detail,
            "tone": weight_tone,
            "icon": "weight",
        },
        {
            "key": "steps",
            "label": "Средние шаги",
            "value": _integer_label(average_steps) if average_steps else "—",
            "detail": steps_detail,
            "tone": "good" if step_delta is not None and step_delta >= 0 else "attention",
            "icon": "steps",
        },
    ]

    return {
        "headline": headline,
        "summary": summary,
        "signals": signals,
        "trends": trends,
        "actions": actions,
        "meta": {
            "latest_daily_date": daily[0].get("date") if daily else None,
            "latest_nutrition_date": nutrition[0].get("date") if nutrition else None,
            "sleep_debt_min": sleep_debt,
        },
    }
