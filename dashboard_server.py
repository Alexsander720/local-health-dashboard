"""
Локальный HTTP-сервер для health dashboard.

Отдаёт dashboard.html и JSON API для интерактива:
    GET  /                       → dashboard.html (билдится на лету)
    GET  /api/data               → latest_sync.json
    GET  /api/ai?period=week     → кэш Gemini (day/week/month)
    POST /api/ai/refresh         → форсированный перезапуск Gemini {period: "week"}
    POST /api/note               → добавить manual note {date, text, tags, replace}
    POST /api/sync               → запустить health_sync.py
    GET  /api/status             → статус (кэши, phone reachable)

Запуск:
    python dashboard_server.py             # порт 8787, открывает браузер
    python dashboard_server.py --port 9999
    python dashboard_server.py --no-open
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import storage_utils
from health_dashboard.domain.body import normalize_measurements, validate_measurements
from health_dashboard.demo_data import (
    build_demo_ai_caches,
    build_demo_data,
    build_demo_food_profile,
    build_demo_measurements,
)
from runtime_state import JobRegistry, build_source_status

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))

DATA_DIR = BASE / "sleep-data"
JSON_PATH = DATA_DIR / "latest_sync.json"
NOTES_PATH = BASE / "manual_notes.json"
MEASUREMENTS_PATH = BASE / "body_measurements.json"

MEASUREMENT_FIELDS = (
    "chest_cm", "shoulders_cm", "waist_cm", "hips_cm",
    "biceps_cm", "thigh_cm", "calf_cm", "neck_cm",
    "arm_length_cm", "foot_cm",
)
MEASUREMENT_LABELS = {
    "chest_cm": "Грудь (объём)",
    "shoulders_cm": "Плечи (ширина)",
    "waist_cm": "Талия",
    "hips_cm": "Бёдра (объём)",
    "biceps_cm": "Бицепс",
    "thigh_cm": "Бедро (объём)",
    "calf_cm": "Икра",
    "neck_cm": "Шея",
    "arm_length_cm": "Рука (длина)",
    "foot_cm": "Стопа (длина)",
}

DEFAULT_PORT = 8787
MAX_JSON_BODY_BYTES = 1_000_000
JOBS = JobRegistry()
DEMO_MODE = False


class RequestBodyTooLarge(ValueError):
    pass


def is_allowed_request_origin(origin: str | None, host: str | None) -> bool:
    if not origin:
        return True
    if not host:
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == host.lower()


def load_notes() -> dict:
    if NOTES_PATH.exists():
        try:
            return json.loads(NOTES_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_notes(notes: dict):
    storage_utils.atomic_write_json(NOTES_PATH, notes)


def load_measurements() -> dict:
    if MEASUREMENTS_PATH.exists():
        try:
            raw = json.loads(MEASUREMENTS_PATH.read_text(encoding="utf-8"))
            fallback_date = datetime.fromtimestamp(
                MEASUREMENTS_PATH.stat().st_mtime
            ).strftime("%Y-%m-%d")
            return normalize_measurements(raw, fallback_date=fallback_date)
        except Exception:
            pass
    return {}


def save_measurements(data: dict):
    storage_utils.atomic_write_json(MEASUREMENTS_PATH, validate_measurements(data))


def build_chat_system_prompt(data: dict, current_time: str | None = None) -> str:
    from gemini_analyzer import build_payload

    current_time = current_time or datetime.now().strftime("%Y-%m-%d %H:%M")
    payload = build_payload(data, "chat")
    return (
        "Ты персональный AI-ассистент по здоровью. У тебя есть сводная выгрузка пользователя "
        "за весь доступный период, а не только за неделю. Смотри поле data_coverage перед тем, "
        "как говорить, какие данные есть или отсутствуют.\n"
        f"ТЕКУЩЕЕ ВРЕМЯ И ДАТА СЕЙЧАС: {current_time}.\n"
        "Отвечай конкретно, с цифрами из данных. Пиши на русском. "
        "Используй HTML-форматирование: <p>, <strong>, <ul>, <li>, <em>. НЕ используй markdown.\n"
        "Если пользователь спрашивает про месяц или более старый период, используй все доступные "
        "строки nutrition, daily_metrics, workouts и weight_history из payload. Не заявляй, что "
        "видишь только 7 или 14 дней, если data_coverage показывает более ранние записи.\n"
        "Если для точного расчета не хватает целей калорий или расхода, честно отделяй точный "
        "расчет от оценки по весу/активности.\n"
        "Будь краток, но точен. Если пользователь спрашивает о трендах — считай средние, "
        "сравнивай периоды и называй даты.\n"
        "Обращай внимание на время сообщений из истории: если пользователь говорит 'вчера', "
        "отсчитывай от времени текущего сообщения.\n\n"
        f"ДАННЫЕ ПОЛЬЗОВАТЕЛЯ:\n{json.dumps(payload, ensure_ascii=False)}"
    )


def delete_manual_note(notes: dict, date: str, *, added_at=None, index=None, all_for_date: bool = False) -> dict:
    entries = notes.get(date)
    if not date or not entries:
        return {"ok": False, "error": "not found"}

    if all_for_date:
        deleted = list(entries)
        del notes[date]
        return {"ok": True, "deleted": deleted, "remaining": 0}

    target = None
    if added_at:
        for i, entry in enumerate(entries):
            if entry.get("added_at") == added_at:
                target = i
                break
    elif index is not None:
        try:
            idx = int(index)
            if 0 <= idx < len(entries):
                target = idx
        except (TypeError, ValueError):
            target = None
    elif len(entries) == 1:
        target = 0
    else:
        return {"ok": False, "error": "selector required"}

    if target is None:
        return {"ok": False, "error": "not found"}

    deleted = entries.pop(target)
    if entries:
        notes[date] = entries
        remaining = len(entries)
    else:
        del notes[date]
        remaining = 0
    return {"ok": True, "deleted": deleted, "remaining": remaining}


def run_sync() -> dict:
    """Запустить health_sync.py и вернуть статистику."""
    env = {**os.environ, "PYTHONUTF8": "1"}
    try:
        r = subprocess.run(
            [
                sys.executable,
                str(BASE / "health_sync.py"),
                "--days",
                "14",
                "--wake-mobvoi",
            ],
            capture_output=True, text=True, timeout=90, env=env,
        )
        last_line = (r.stdout or "").strip().splitlines()[-1] if r.stdout else ""
        result = {
            "ok": r.returncode == 0,
            "last": last_line,
            "stderr": (r.stderr or "")[-500:],
            "rebuilt": False,
        }
        if r.returncode == 0:
            try:
                from build_dashboard import build
                dashboard_path = build(include_ai=True, server_mode=False)
                result["rebuilt"] = True
                result["dashboard"] = str(dashboard_path)
            except Exception as e:
                result["ok"] = False
                result["rebuild_error"] = str(e)
        return result
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}")

    # --- ответы ---
    def _send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid content length") from exc
        if length < 0:
            raise ValueError("invalid content length")
        if length > MAX_JSON_BODY_BYTES:
            raise RequestBodyTooLarge("request body too large")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid json") from exc
        if not isinstance(body, dict):
            raise ValueError("json body must be an object")
        return body

    # --- routes ---
    def do_GET(self):
        path = self.path.split("?")[0]
        query = {}
        if "?" in self.path:
            from urllib.parse import parse_qs
            query = {k: v[0] for k, v in parse_qs(self.path.split("?", 1)[1]).items()}

        if path == "/" or path == "/index.html":
            return self.handle_index()
        if path == "/api/data":
            return self.handle_data()
        if path == "/api/ai":
            return self.handle_ai_get(query.get("period", "week"))
        if path == "/api/status":
            return self.handle_status()
        if path == "/api/notes":
            return self.handle_notes_list()
        if path == "/api/food-profile":
            return self.handle_food_profile_get()
        if path == "/api/measurements":
            return self.handle_measurements_get()

        return self._json(404, {"error": "not found", "path": path})

    def do_POST(self):
        path = self.path.split("?")[0]
        if not is_allowed_request_origin(self.headers.get("Origin"), self.headers.get("Host")):
            return self._json(403, {"error": "cross-origin request rejected"})
        if DEMO_MODE:
            return self._json(403, {"error": "demo mode is read-only"})
        try:
            body = self._read_json()
        except RequestBodyTooLarge:
            return self._json(413, {"error": "request body too large"})
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})

        if path == "/api/ai/refresh":
            return self.handle_ai_refresh(body)
        if path == "/api/note":
            return self.handle_note_post(body)
        if path == "/api/note/delete":
            return self.handle_note_delete(body)
        if path == "/api/food-profile":
            return self.handle_food_profile_post(body)
        if path == "/api/sync":
            return self.handle_sync_post()
        if path == "/api/chat":
            return self.handle_chat(body)
        if path == "/api/symptom_acute":
            return self.handle_symptom_acute(body)
        if path == "/api/measurements":
            return self.handle_measurements_post(body)

        return self._json(404, {"error": "not found", "path": path})

    def do_OPTIONS(self):
        if not is_allowed_request_origin(self.headers.get("Origin"), self.headers.get("Host")):
            return self._json(403, {"error": "cross-origin request rejected"})
        self._send(204, b"", "text/plain; charset=utf-8")

    # --- handlers ---
    def handle_index(self):
        try:
            from build_dashboard import render_html
            body = render_html(
                include_ai=True,
                server_mode=True,
                demo_mode=DEMO_MODE,
            ).encode("utf-8")
            self._send(200, body)
        except Exception:
            import traceback
            traceback.print_exc()
            self._send(500, b"Dashboard render failed")

    def handle_data(self):
        if DEMO_MODE:
            return self._json(200, build_demo_data())
        if not JSON_PATH.exists():
            return self._json(404, {"error": "no sync yet"})
        data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
        self._json(200, data)

    def handle_ai_get(self, period: str):
        if DEMO_MODE:
            cache = build_demo_ai_caches().get(period)
            if not cache:
                return self._json(400, {"error": "invalid period"})
            return self._json(
                200,
                {
                    "period": period,
                    "text": cache["text"],
                    "meta": {"iso": cache["iso"], "model": cache["model"]},
                },
            )
        try:
            from gemini_analyzer import load_cache, CACHE_PATHS
            if period not in CACHE_PATHS:
                return self._json(400, {"error": "invalid period"})
            text, meta = load_cache(period)
            if not text:
                return self._json(404, {"error": "no cache", "period": period})
            self._json(200, {"period": period, "text": text, "meta": meta})
        except Exception as e:
            self._json(500, {"error": str(e)})

    def handle_ai_refresh(self, body: dict):
        period = body.get("period", "week")
        try:
            from gemini_analyzer import analyze, CACHE_PATHS, load_cache
            if period not in CACHE_PATHS:
                return self._json(400, {"ok": False, "error": "invalid period"})
            outcome = JOBS.run(
                f"ai:{period}",
                lambda: analyze(period=period, force=True),
            )
            if not outcome["accepted"]:
                return self._json(409, {
                    "ok": False,
                    "busy": True,
                    "error": "analysis already running",
                    "job": outcome["job"],
                })
            text = outcome["result"]
            _, meta = load_cache(period)
            self._json(200, {
                "ok": True,
                "period": period,
                "chars": len(text),
                "text": text,
                "model": (meta or {}).get("model"),
            })
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)})

    def handle_status(self):
        if DEMO_MODE:
            data = build_demo_data()
            caches = build_demo_ai_caches()
            return self._json(
                200,
                {
                    "demo": True,
                    "synced_at": data["synced_at"],
                    "ai_cache": {
                        period: {
                            "iso": cache["iso"],
                            "age_sec": 0,
                            "model": cache["model"],
                        }
                        for period, cache in caches.items()
                    },
                    "notes_count": len(data["feelings"]),
                    "jobs": {},
                    "sources": {
                        "synthetic_demo": {
                            "label": "Synthetic demo",
                            "state": "fresh",
                            "records": sum(
                                len(data.get(key) or [])
                                for key in (
                                    "sleep_sessions",
                                    "daily_metrics",
                                    "weight_history",
                                    "nutrition",
                                    "workouts",
                                )
                            ),
                            "last_record_at": data["synced_at"],
                            "age_hours": 0,
                            "stale_after_hours": 8760,
                        }
                    },
                    "now": datetime.now().isoformat(timespec="seconds"),
                },
            )
        from gemini_analyzer import CACHE_PATHS, load_cache
        status = {
            "synced_at": None,
            "ai_cache": {},
            "notes_count": 0,
            "jobs": JOBS.snapshot(),
            "sources": {},
            "now": datetime.now().isoformat(timespec="seconds"),
        }
        if JSON_PATH.exists():
            try:
                d = json.loads(JSON_PATH.read_text(encoding="utf-8"))
                status["synced_at"] = d.get("synced_at")
                status["sources"] = build_source_status(d)
            except Exception:
                pass
        for period in CACHE_PATHS:
            _, meta = load_cache(period)
            if meta:
                status["ai_cache"][period] = {
                    "iso": meta.get("iso"),
                    "age_sec": int(datetime.now().timestamp() - meta.get("timestamp", 0)),
                    "model": meta.get("model"),
                }
        status["notes_count"] = sum(len(v) for v in load_notes().values())
        self._json(200, status)

    def handle_notes_list(self):
        if DEMO_MODE:
            notes = []
            for feeling in build_demo_data().get("feelings", []):
                text = feeling.get("manual_note") or feeling.get("yazio_note")
                if text:
                    notes.append(
                        {
                            "date": feeling["date"],
                            "text": text,
                            "tags": feeling.get("manual_tags") or feeling.get("yazio_tags") or [],
                            "source": "synthetic-demo",
                        }
                    )
            return self._json(200, notes)
        notes = load_notes()
        out = []
        for date in sorted(notes.keys(), reverse=True):
            for e in notes[date]:
                out.append({"date": date, **e})
        self._json(200, out)

    def handle_note_post(self, body: dict):
        date = body.get("date") or datetime.now().strftime("%Y-%m-%d")
        text = (body.get("text") or "").strip()
        tags = body.get("tags") or []
        replace = body.get("replace", True)
        if not text:
            return self._json(400, {"error": "empty text"})
        notes = load_notes()
        entry = {
            "text": text,
            "time": datetime.now().strftime("%H:%M"),
            "tags": tags,
            "added_at": datetime.now().isoformat(timespec="seconds"),
        }
        if replace:
            notes[date] = [entry]
        else:
            notes.setdefault(date, []).append(entry)
        save_notes(notes)
        self._json(200, {"ok": True, "date": date, "chars": len(text)})

    def handle_note_delete(self, body: dict):
        date = body.get("date")
        if not date:
            return self._json(400, {"error": "no date"})
        notes = load_notes()
        result = delete_manual_note(
            notes,
            date,
            added_at=body.get("added_at"),
            index=body.get("index"),
            all_for_date=bool(body.get("all")),
        )
        if result.get("ok"):
            save_notes(notes)
            return self._json(200, result)
        status = 400 if result.get("error") == "selector required" else 404
        self._json(status, result)

    def handle_food_profile_get(self):
        if DEMO_MODE:
            return self._json(200, {"ok": True, "profile": build_demo_food_profile()})
        try:
            from build_dashboard import load_food_profile
            self._json(200, {"ok": True, "profile": load_food_profile()})
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)})

    def handle_food_profile_post(self, body: dict):
        try:
            from build_dashboard import save_food_profile
            profile = save_food_profile(body)
            self._json(200, {"ok": True, "profile": profile})
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)})

    def handle_sync_post(self):
        outcome = JOBS.run("sync", run_sync)
        if not outcome["accepted"]:
            return self._json(409, {
                "ok": False,
                "busy": True,
                "error": "sync already running",
                "job": outcome["job"],
            })
        self._json(200, outcome["result"])

    def handle_measurements_get(self):
        data = build_demo_measurements() if DEMO_MODE else load_measurements()
        # Return sorted by date desc
        sorted_dates = sorted(data.keys(), reverse=True)
        out = [{"date": d, **data[d]} for d in sorted_dates]
        self._json(200, {"ok": True, "measurements": out, "labels": MEASUREMENT_LABELS, "fields": list(MEASUREMENT_FIELDS)})

    def handle_measurements_post(self, body: dict):
        date = body.get("date") or datetime.now().strftime("%Y-%m-%d")
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except Exception:
            return self._json(400, {"ok": False, "error": "invalid date (use YYYY-MM-DD)"})

        entry: dict = {}
        for f in MEASUREMENT_FIELDS:
            v = body.get(f)
            if v is None or v == "":
                continue
            try:
                entry[f] = float(v)
            except (TypeError, ValueError):
                return self._json(400, {"ok": False, "error": f"{f}: должно быть число"})
        if not entry:
            return self._json(400, {"ok": False, "error": "ни одного поля не задано"})

        entry["added_at"] = datetime.now().isoformat(timespec="seconds")
        data = load_measurements()
        # При повторе на ту же дату — мерж (новые поля пересиливают)
        existing = data.get(date) or {}
        existing.update(entry)
        data[date] = existing
        save_measurements(data)
        self._json(200, {"ok": True, "date": date, "entry": existing, "total_dates": len(data)})

    def handle_symptom_acute(self, body: dict):
        symptom = (body.get("symptom") or "").strip()
        if not symptom:
            return self._json(400, {"ok": False, "error": "пустой симптом"})
        if len(symptom) > 500:
            return self._json(400, {"ok": False, "error": "слишком длинный текст (>500 символов)"})
        try:
            from gemini_analyzer import analyze_symptom
            result = analyze_symptom(symptom)
            self._json(200, result)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._json(500, {"ok": False, "error": str(e)})

    def handle_chat(self, body: dict):
        message = (body.get("message") or "").strip()
        history = body.get("history") or []
        if not message:
            return self._json(400, {"error": "empty message"})
        try:
            from gemini_analyzer import (
                get_access_token, get_project, FALLBACK_MODELS, GEMINI_31_PRO,
            )
            token = get_access_token()
            if not token:
                return self._json(500, {"ok": False, "error": "OAuth не настроен. Выполни: gcloud auth login"})
            project = get_project()

            # Build health data context
            data = json.loads(JSON_PATH.read_text(encoding="utf-8")) if JSON_PATH.exists() else {}
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M")
            system_prompt = build_chat_system_prompt(data, current_time)

            # Build conversation contents for Gemini
            contents = [{"role": "user", "parts": [{"text": system_prompt}]}]
            contents.append({"role": "model", "parts": [{"text": f"[Система: текущее время {current_time}] Понял, я готов отвечать на вопросы по твоим данным здоровья с учетом времени."}]})

            for msg in history[-20:]:  # last 20 messages for context
                role = "user" if msg.get("role") == "user" else "model"
                time_prefix = f"[{msg.get('timestamp')}] " if msg.get("timestamp") else ""
                contents.append({"role": role, "parts": [{"text": time_prefix + msg.get("text", "")}]})

            time_prefix = f"[{current_time}] "
            contents.append({"role": "user", "parts": [{"text": time_prefix + message}]})

            # Call Gemini
            import urllib.request
            import urllib.error

            models = [GEMINI_31_PRO] + [m for m in FALLBACK_MODELS if m != GEMINI_31_PRO]
            errors = []
            for model in models:
                try:
                    from gemini_analyzer import model_location, endpoint_host
                    location = model_location(model)
                    host = endpoint_host(location)
                    url = (
                        f"https://{host}/v1/projects/{project}"
                        f"/locations/{location}/publishers/google/models/{model}:generateContent"
                    )
                    gen_config = {"maxOutputTokens": 4000}
                    if model.startswith("gemini-3"):
                        gen_config["thinkingConfig"] = {"thinkingLevel": "MEDIUM"}
                    else:
                        gen_config["temperature"] = 0.5
                    req_body = json.dumps({
                        "contents": contents,
                        "generationConfig": gen_config,
                    }).encode("utf-8")
                    req = urllib.request.Request(url, data=req_body, headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {token}",
                    })
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        result = json.loads(resp.read())
                    cands = result.get("candidates", [])
                    if cands:
                        parts = cands[0].get("content", {}).get("parts", [])
                        text = "".join(p.get("text", "") for p in parts).strip()
                        if text:
                            return self._json(200, {"ok": True, "reply": text, "model": model})
                except urllib.error.HTTPError as e:
                    errors.append(f"{model}: HTTP {e.code}")
                except Exception as e:
                    errors.append(f"{model}: {e}")

            return self._json(500, {"ok": False, "error": "Gemini unavailable: " + "; ".join(errors[:2])})
        except Exception as e:
            import traceback
            return self._json(500, {"ok": False, "error": str(e)})


def main():
    global DEMO_MODE
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--demo", action="store_true", help="Serve synthetic data in read-only mode")
    args = parser.parse_args()
    DEMO_MODE = args.demo

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Dashboard server: {url}")
    if DEMO_MODE:
        print("Mode: synthetic read-only demo")
    print("Routes: /api/data /api/ai?period= /api/ai/refresh /api/note /api/sync")

    if not args.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
