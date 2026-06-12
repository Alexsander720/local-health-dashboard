"""
Извлечение данных о сне из Mobvoi Health (TicWatch) через ADB + root.

Подключение: телефон по USB, root (Magisk), ADB разрешён.
База: /data/data/com.mobvoi.companion.at/databases/health_common.db

Использование:
    python extract_sleep.py                  # последние 7 дней
    python extract_sleep.py --days 30        # последние 30 дней
    python extract_sleep.py --all            # все данные
    python extract_sleep.py --output report  # сохранить в файл
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ADB = r"C:\scrcpy-win64-v3.3.4\adb.exe"
MOBVOI_DB = "/data/data/com.mobvoi.companion.at/databases/health_common.db"
SNORE_DIR = "/sdcard/Android/data/com.mobvoi.companion.at/files/snore_log"

# Типы данных в health_common.db
TYPE_HR = 1           # ЧСС (каждую минуту)
TYPE_SLEEP_STAGE = 2  # Стадии сна: "stage,next_stage"
TYPE_SPO2 = 28        # Сатурация кислорода
TYPE_STRESS = 29      # Уровень стресса
TYPE_RESP_RATE = 38   # Частота дыхания
TYPE_TEMP = 42        # Температура кожи

# Коды стадий сна Mobvoi
STAGE_AWAKE = 7
STAGE_REM = 8
STAGE_LIGHT = 9
STAGE_DEEP = 10
STAGE_BOUNDARY = 99

STAGE_NAMES = {
    STAGE_AWAKE: "awake",
    STAGE_REM: "rem",
    STAGE_LIGHT: "light",
    STAGE_DEEP: "deep",
    STAGE_BOUNDARY: "boundary",
}

MSK = timezone(timedelta(hours=3))


def adb(*args: str) -> str:
    """Выполнить ADB команду, вернуть stdout."""
    cmd = [ADB] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                            env={**os.environ, "MSYS_NO_PATHCONV": "1"})
    if result.returncode != 0 and "error" in result.stderr.lower():
        raise RuntimeError(f"ADB error: {result.stderr.strip()}")
    return result.stdout.strip()


def adb_shell(command: str) -> str:
    """Выполнить shell команду через ADB."""
    return adb("shell", command)


def adb_root_shell(command: str) -> str:
    """Выполнить shell команду через su."""
    return adb("shell", f"su -c '{command}'")


def pull_database(local_dir: str) -> str:
    """Скопировать БД Mobvoi на ПК через root."""
    tmp_remote = "/sdcard/_health_common.db"
    local_path = os.path.join(local_dir, "health_common.db")

    # Чекпоинт WAL чтобы все данные были в основном файле
    adb_root_shell(
        f'sqlite3 {MOBVOI_DB} "PRAGMA wal_checkpoint(TRUNCATE)"'
    )
    adb_root_shell(f"cp {MOBVOI_DB} {tmp_remote}")
    adb("pull", tmp_remote, local_path)
    adb_shell(f"rm {tmp_remote}")

    print(f"  БД скопирована: {local_path}")
    return local_path


def pull_snore_logs(local_dir: str) -> dict[str, str]:
    """Скачать логи храпа."""
    snore_dir = os.path.join(local_dir, "snore_logs")
    os.makedirs(snore_dir, exist_ok=True)
    try:
        adb("pull", SNORE_DIR + "/", snore_dir)
    except Exception:
        pass
    logs = {}
    for f in Path(snore_dir).glob("*.txt"):
        logs[f.stem] = f.read_text(encoding="utf-8", errors="replace")
    return logs


def ts_to_dt(ms: int) -> datetime:
    """Timestamp в миллисекундах -> datetime MSK."""
    return datetime.fromtimestamp(ms / 1000, tz=MSK)


def dt_to_iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def extract_sessions(conn: sqlite3.Connection, since_ms: int | None) -> list[dict]:
    """Извлечь сессии сна из data_session (type=100)."""
    c = conn.cursor()
    query = "SELECT id, time_from, time_to FROM data_session WHERE type=100 AND deleted=0"
    params = []
    if since_ms:
        query += " AND time_from >= ?"
        params.append(since_ms)
    query += " ORDER BY time_from DESC"
    c.execute(query, params)

    sessions = []
    for row in c.fetchall():
        sessions.append({
            "id": row[0],
            "start_ms": row[1],
            "end_ms": row[2],
            "start": dt_to_iso(ts_to_dt(row[1])),
            "end": dt_to_iso(ts_to_dt(row[2])),
        })
    return sessions


def extract_sleep_stages(conn: sqlite3.Connection, start_ms: int, end_ms: int) -> list[dict]:
    """Извлечь стадии сна для сессии."""
    c = conn.cursor()
    c.execute(
        'SELECT "values", time_from, time_to FROM data_point '
        'WHERE type=? AND time_from >= ? AND time_to <= ? ORDER BY time_from',
        (TYPE_SLEEP_STAGE, start_ms, end_ms)
    )

    stages = []
    for row in c.fetchall():
        vals = row[0].split(",")
        stage_code = int(vals[0]) if vals else -1
        stage_name = STAGE_NAMES.get(stage_code, f"unknown_{stage_code}")
        duration_min = (row[2] - row[1]) / 60000

        if stage_code in (STAGE_AWAKE, STAGE_REM, STAGE_LIGHT, STAGE_DEEP):
            stages.append({
                "stage": stage_name,
                "stage_code": stage_code,
                "start": dt_to_iso(ts_to_dt(row[1])),
                "end": dt_to_iso(ts_to_dt(row[2])),
                "duration_min": round(duration_min, 1),
            })
    return stages


def compute_stage_summary(stages: list[dict], total_min: float) -> dict:
    """Посчитать процент каждой стадии."""
    totals = {"awake": 0.0, "rem": 0.0, "light": 0.0, "deep": 0.0}
    for s in stages:
        name = s["stage"]
        if name in totals:
            totals[name] += s["duration_min"]

    sleep_min = totals["rem"] + totals["light"] + totals["deep"]
    result = {}
    for name, minutes in totals.items():
        pct = (minutes / total_min * 100) if total_min > 0 else 0
        result[name] = {
            "minutes": round(minutes, 1),
            "formatted": f"{int(minutes // 60)}h {int(minutes % 60)}m",
            "percent": round(pct, 1),
        }
    result["total_sleep_min"] = round(sleep_min, 1)
    result["total_in_bed_min"] = round(total_min, 1)
    return result


def extract_metric(conn: sqlite3.Connection, type_id: int, start_ms: int, end_ms: int,
                   as_float: bool = False) -> dict | None:
    """Извлечь метрику (HR, SpO2 и т.д.) за период сна."""
    c = conn.cursor()
    cast = "REAL" if as_float else "INTEGER"
    c.execute(
        f'SELECT COUNT(*), MIN(CAST("values" AS {cast})), '
        f'MAX(CAST("values" AS {cast})), AVG(CAST("values" AS {cast})) '
        f'FROM data_point WHERE type=? AND time_from >= ? AND time_from <= ?',
        (type_id, start_ms, end_ms)
    )
    row = c.fetchone()
    if not row or row[0] == 0:
        return None

    fmt = (lambda x: round(x, 1)) if as_float else (lambda x: int(x))
    return {
        "count": row[0],
        "min": fmt(row[1]),
        "max": fmt(row[2]),
        "avg": fmt(row[3]) if not as_float else round(row[3], 1),
    }


def extract_metric_series(conn: sqlite3.Connection, type_id: int, start_ms: int, end_ms: int,
                          as_float: bool = False) -> list[dict]:
    """Извлечь временной ряд метрики."""
    c = conn.cursor()
    c.execute(
        'SELECT "values", time_from FROM data_point '
        'WHERE type=? AND time_from >= ? AND time_from <= ? ORDER BY time_from',
        (type_id, start_ms, end_ms)
    )
    series = []
    for row in c.fetchall():
        val = float(row[0]) if as_float else int(row[0])
        series.append({
            "time": dt_to_iso(ts_to_dt(row[1])),
            "value": round(val, 1) if as_float else val,
        })
    return series


def estimate_quality(stage_summary: dict, hr_stats: dict | None, spo2_stats: dict | None) -> int:
    """Грубая оценка качества сна (0-100), аналог Mobvoi."""
    score = 50  # базовый

    total = stage_summary.get("total_in_bed_min", 0)
    if total == 0:
        return 0

    # Длительность сна (оптимум 7-9 часов)
    sleep_hours = stage_summary.get("total_sleep_min", 0) / 60
    if 7 <= sleep_hours <= 9:
        score += 15
    elif 6 <= sleep_hours < 7 or 9 < sleep_hours <= 10:
        score += 8
    elif sleep_hours < 5:
        score -= 10

    # Глубокий сон (оптимум 15-25%)
    deep_pct = stage_summary.get("deep", {}).get("percent", 0)
    if 15 <= deep_pct <= 25:
        score += 15
    elif 10 <= deep_pct < 15 or 25 < deep_pct <= 30:
        score += 8
    elif deep_pct < 5:
        score -= 5

    # REM (оптимум 20-25%)
    rem_pct = stage_summary.get("rem", {}).get("percent", 0)
    if 18 <= rem_pct <= 28:
        score += 10
    elif 10 <= rem_pct < 18:
        score += 5

    # Пробуждения (меньше = лучше)
    awake_pct = stage_summary.get("awake", {}).get("percent", 0)
    if awake_pct <= 5:
        score += 10
    elif awake_pct <= 10:
        score += 5
    else:
        score -= 5

    # SpO2
    if spo2_stats and spo2_stats["min"] >= 95:
        score += 5
    elif spo2_stats and spo2_stats["min"] < 90:
        score -= 10

    return max(0, min(100, score))


def process_session(conn: sqlite3.Connection, session: dict, include_series: bool = False) -> dict:
    """Обработать одну сессию сна — все метрики."""
    start_ms = session["start_ms"]
    end_ms = session["end_ms"]
    total_min = (end_ms - start_ms) / 60000

    stages = extract_sleep_stages(conn, start_ms, end_ms)
    stage_summary = compute_stage_summary(stages, total_min)

    hr = extract_metric(conn, TYPE_HR, start_ms, end_ms)
    spo2 = extract_metric(conn, TYPE_SPO2, start_ms, end_ms)
    stress = extract_metric(conn, TYPE_STRESS, start_ms, end_ms)
    resp_rate = extract_metric(conn, TYPE_RESP_RATE, start_ms, end_ms)
    temp = extract_metric(conn, TYPE_TEMP, start_ms, end_ms, as_float=True)

    quality = estimate_quality(stage_summary, hr, spo2)

    result = {
        "date": ts_to_dt(start_ms).strftime("%Y-%m-%d"),
        "start": session["start"],
        "end": session["end"],
        "duration": f"{int(total_min // 60)}h {int(total_min % 60)}m",
        "duration_min": round(total_min, 1),
        "quality_score": quality,
        "stages": stage_summary,
        "stage_timeline": stages,
        "heart_rate": hr,
        "spo2": spo2,
        "stress": stress,
        "respiratory_rate": resp_rate,
        "skin_temperature": temp,
    }

    if include_series:
        result["hr_series"] = extract_metric_series(conn, TYPE_HR, start_ms, end_ms)
        result["spo2_series"] = extract_metric_series(conn, TYPE_SPO2, start_ms, end_ms)
        result["temp_series"] = extract_metric_series(conn, TYPE_TEMP, start_ms, end_ms, as_float=True)

    return result


def format_console(sessions: list[dict]) -> str:
    """Форматировать для вывода в консоль."""
    lines = []
    for s in sessions:
        lines.append(f"\n{'='*60}")
        t_start = s['start'][11:16]
        t_end = s['end'][11:16]
        lines.append(f"  {s['date']}  |  {t_start} - {t_end}  |  {s['duration']}")
        lines.append(f"  Качество: {s['quality_score']}/100")
        lines.append(f"{'='*60}")

        st = s["stages"]
        lines.append(f"  Стадии сна:")
        for name, label in [("deep", "Глубокий"), ("rem", "REM"), ("light", "Лёгкий"), ("awake", "Бодрствование")]:
            d = st.get(name, {})
            lines.append(f"    {label:15s} {d.get('percent', 0):5.1f}%  ({d.get('formatted', '-')})")
        lines.append(f"    {'Всего сон':15s} {st.get('total_sleep_min', 0):.0f} мин")

        if s.get("heart_rate"):
            hr = s["heart_rate"]
            lines.append(f"  ЧСС:          мин {hr['min']}  макс {hr['max']}  средн {hr['avg']} уд/мин  ({hr['count']} замеров)")

        if s.get("spo2"):
            sp = s["spo2"]
            lines.append(f"  SpO2:          мин {sp['min']}%  макс {sp['max']}%  средн {sp['avg']}%")

        if s.get("stress"):
            st2 = s["stress"]
            lines.append(f"  Стресс:        мин {st2['min']}  макс {st2['max']}  средн {st2['avg']}")

        if s.get("respiratory_rate"):
            rr = s["respiratory_rate"]
            lines.append(f"  Дыхание:       мин {rr['min']}  макс {rr['max']}  средн {rr['avg']} вд/мин")

        if s.get("skin_temperature"):
            t = s["skin_temperature"]
            lines.append(f"  Температура:   мин {t['min']}°  макс {t['max']}°  средн {t['avg']}°C")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Извлечение данных о сне из TicWatch/Mobvoi")
    parser.add_argument("--days", type=int, default=7, help="За сколько дней (по умолчанию 7)")
    parser.add_argument("--all", action="store_true", help="Все данные")
    parser.add_argument("--output", "-o", type=str, help="Имя файла для JSON (без расширения)")
    parser.add_argument("--series", action="store_true", help="Включить временные ряды (HR, SpO2, темп)")
    parser.add_argument("--db", type=str, help="Путь к локальной БД (пропустить ADB)")
    args = parser.parse_args()

    script_dir = Path(__file__).parent
    work_dir = script_dir / "sleep-data"
    work_dir.mkdir(exist_ok=True)

    # Получить БД
    if args.db:
        db_path = args.db
        print(f"Используется локальная БД: {db_path}")
    else:
        print("Подключение к телефону...")
        devices = adb("devices")
        if "device" not in devices.split("\n", 1)[-1]:
            print("Ошибка: телефон не подключён по ADB")
            sys.exit(1)
        print("  Телефон найден")

        print("Копирование БД Mobvoi Health...")
        db_path = pull_database(str(work_dir))

    # Открыть БД
    conn = sqlite3.connect(db_path)

    since_ms = None
    if not args.all:
        since_dt = datetime.now(tz=MSK) - timedelta(days=args.days)
        since_ms = int(since_dt.timestamp() * 1000)
        print(f"Период: последние {args.days} дней (с {since_dt.strftime('%Y-%m-%d')})")

    # Извлечь сессии
    print("Извлечение сессий сна...")
    sessions = extract_sessions(conn, since_ms)
    print(f"  Найдено сессий: {len(sessions)}")

    if not sessions:
        print("Нет данных о сне за указанный период.")
        conn.close()
        return

    # Обработать каждую сессию
    print("Обработка данных...")
    results = []
    for s in sessions:
        result = process_session(conn, s, include_series=args.series)
        results.append(result)

    conn.close()

    # Вывод в консоль
    print(format_console(results))

    # Сохранение JSON
    if args.output:
        out_path = work_dir / f"{args.output}.json"
    else:
        out_path = work_dir / f"sleep_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    export = {
        "exported_at": datetime.now(tz=MSK).isoformat(timespec="seconds"),
        "period_days": "all" if args.all else args.days,
        "sessions_count": len(results),
        "sessions": results,
    }
    out_path.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nJSON сохранён: {out_path}")

    # Краткая сводка
    print(f"\n{'─'*60}")
    print("СВОДКА:")
    avg_quality = sum(r["quality_score"] for r in results) / len(results)
    avg_duration = sum(r["duration_min"] for r in results) / len(results)
    avg_deep = sum(r["stages"].get("deep", {}).get("percent", 0) for r in results) / len(results)
    avg_rem = sum(r["stages"].get("rem", {}).get("percent", 0) for r in results) / len(results)
    print(f"  Среднее качество:    {avg_quality:.0f}/100")
    print(f"  Средняя длительность: {avg_duration / 60:.1f}ч")
    print(f"  Средний глубокий:    {avg_deep:.1f}%")
    print(f"  Средний REM:         {avg_rem:.1f}%")
    print(f"{'─'*60}")


if __name__ == "__main__":
    main()
