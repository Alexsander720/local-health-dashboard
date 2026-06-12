"""
Быстрая добавлялка/редактор заметок здоровья.

Хранит в manual_notes.json (ключ = дата). Объединяется с YAZIO feelings
в health_sync.py → дашборд и Gemini видят обе заметки как одну.

Использование:
    python note.py                              # редактор для СЕГОДНЯ (с YAZIO контекстом)
    python note.py "короткая заметка"           # добавить одной строкой
    python note.py "текст" --tag sick --tag hr
    python note.py --date 2026-04-16            # редактор для другой даты
    python note.py --date 2026-04-16 "текст"    # добавить на другую дату
    python note.py --list                       # последние 10
    python note.py --delete 2026-04-17          # удалить manual за дату
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import storage_utils

BASE = Path(__file__).parent
NOTES_PATH = BASE / "manual_notes.json"
SYNC_JSON = BASE / "sleep-data" / "latest_sync.json"

SEPARATOR = "━" * 60
EDITOR_HEADER = "# Строки, начинающиеся с #, игнорируются при сохранении."


def load_notes() -> dict:
    if NOTES_PATH.exists():
        try:
            return json.loads(NOTES_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_notes(notes: dict):
    storage_utils.atomic_write_json(NOTES_PATH, notes)


def load_yazio_note(date: str) -> dict | None:
    """Читает YAZIO заметку за дату из latest_sync.json."""
    if not SYNC_JSON.exists():
        return None
    try:
        data = json.loads(SYNC_JSON.read_text(encoding="utf-8"))
        for f in data.get("feelings", []):
            fd = (f.get("date") or "").strip('"')
            if fd == date:
                note = f.get("yazio_note") or (f.get("note") if f.get("source") == "yazio" else None)
                if note:
                    return {"note": note, "tags": f.get("yazio_tags") or f.get("tags") or []}
        return None
    except Exception:
        return None


def open_editor(initial_text: str) -> str:
    editor = os.environ.get("EDITOR")
    if not editor:
        editor = "notepad" if os.name == "nt" else "nano"

    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt", encoding="utf-8") as f:
        f.write(initial_text)
        tmp = f.name

    try:
        subprocess.call([editor, tmp])
        with open(tmp, encoding="utf-8") as f:
            raw = f.read()
        # Убираем строки-комментарии
        clean_lines = [line for line in raw.splitlines() if not line.lstrip().startswith("#")]
        return "\n".join(clean_lines).strip()
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


def build_editor_template(date: str) -> str:
    """Шаблон для редактора: YAZIO как контекст + текущая manual снизу."""
    lines = [
        EDITOR_HEADER,
        f"# Заметка за {date}",
        "#",
    ]

    yazio = load_yazio_note(date)
    if yazio and yazio.get("note"):
        lines.append("# ━━━ Заметка из YAZIO (только для чтения, редактируй в приложении) ━━━")
        for ln in yazio["note"].splitlines():
            lines.append(f"# {ln}")
        if yazio.get("tags"):
            tags_str = " ".join(f"#{t}" for t in yazio["tags"])
            lines.append(f"# теги: {tags_str}")
        lines.append("#")

    notes = load_notes()
    existing = notes.get(date, [])
    existing_text = ""
    if existing:
        parts = []
        for e in existing:
            t = e.get("time", "")
            txt = e.get("text", "").strip()
            if txt:
                parts.append(f"[{t}] {txt}" if t else txt)
        existing_text = "\n\n".join(parts)

    lines.append("# ━━━ Твоя заметка с ПК (редактируй ниже) ━━━")
    lines.append("")
    return "\n".join(lines) + "\n" + existing_text


def save_for_date(date: str, text: str, tags: list[str], replace: bool = True):
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
    return entry


def append_for_date(date: str, text: str, tags: list[str]):
    return save_for_date(date, text, tags, replace=False)


def list_notes(limit: int = 15):
    notes = load_notes()
    if not notes:
        print("(нет manual-заметок)")
        return
    count = 0
    for date in sorted(notes.keys(), reverse=True):
        yazio = load_yazio_note(date)
        print(f"\n{SEPARATOR}")
        print(f"  {date}")
        print(SEPARATOR)
        if yazio and yazio.get("note"):
            print(f"  [YAZIO] {yazio['note'][:200]}")
            if yazio.get("tags"):
                print(f"  теги: {' '.join('#'+t for t in yazio['tags'])}")
            print()
        for i, n in enumerate(notes[date]):
            tags = " ".join(f"#{t}" for t in n.get("tags", []))
            time = n.get("time", "")
            print(f"  [С ПК {time}] {tags}")
            for line in n["text"].splitlines():
                print(f"    {line}")
            count += 1
            if count >= limit:
                return


def delete_day(date: str):
    notes = load_notes()
    if date in notes:
        del notes[date]
        save_notes(notes)
        print(f"Удалено: {date}")
    else:
        print(f"Нет manual-заметок за {date}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("text", nargs="*")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--delete")
    parser.add_argument("--append", action="store_true",
                        help="Добавить к существующей, не заменять")
    args = parser.parse_args()

    if args.list:
        list_notes()
        return

    if args.delete:
        delete_day(args.delete)
        return

    date = args.date

    if args.text:
        text = " ".join(args.text)
        if args.append:
            append_for_date(date, text, args.tag)
            mode = "добавлено"
        else:
            save_for_date(date, text, args.tag, replace=True)
            mode = "сохранено"
        preview = text[:120].replace("\n", " ")
        tags_str = " ".join(f"#{t}" for t in args.tag) if args.tag else ""
        print(f"OK {date} {mode} {tags_str}: {preview}{'...' if len(text) > 120 else ''} ({len(text)} симв)")
        return

    # Интерактивный режим
    template = build_editor_template(date)
    new_text = open_editor(template)
    if not new_text:
        print("Пусто — не сохранено")
        sys.exit(1)
    save_for_date(date, new_text, args.tag, replace=True)
    print(f"OK {date}: {len(new_text)} символов")


if __name__ == "__main__":
    main()
