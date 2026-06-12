"""
Извлечение Mi cookies из Edge через CDP (Chrome DevTools Protocol).

Edge с конца 2024 шифрует куки в app-bound формате v20, который нельзя
расшифровать без elevation. Workaround: запускаем Edge с
--remote-debugging-port, подключаемся по WebSocket, просим его сам
отдать расшифрованные куки.

Шаги скрипта:
  1. Убивает все процессы Edge (нужен exclusive lock на профиль).
  2. Запускает Edge с --remote-debugging-port=9222 + текущий user data dir.
  3. Подключается к localhost:9222, через CDP вызывает Network.getAllCookies.
  4. Фильтрует mi.com / xiaomi.com куки, сохраняет в mi_cookies.json.
  5. Прибивает Edge.

После этого можно открывать Edge как обычно — никакие настройки не сбиты.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import websocket  # pip install websocket-client


BASE = Path(__file__).parent
USER_DATA_DIR = Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "Edge" / "User Data"
EDGE_EXE = Path(os.environ["PROGRAMFILES(X86)"]) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
if not EDGE_EXE.exists():
    EDGE_EXE = Path(os.environ["PROGRAMFILES"]) / "Microsoft" / "Edge" / "Application" / "msedge.exe"

OUTPUT = BASE / "mi_cookies.json"
DEBUG_PORT = 9222
INTERESTING = {"userId", "serviceToken", "passToken", "cUserId", "ssecurity", "pwdToken", "deviceId"}


def kill_edge():
    """Прибиваем все процессы Edge чтобы освободить профиль."""
    subprocess.run(
        ["taskkill", "/im", "msedge.exe", "/F"],
        capture_output=True,  # bytes only — избегаем cp1251 decode crash
    )
    time.sleep(1.5)


def start_edge_debug() -> subprocess.Popen:
    """Запускаем Edge с remote debugging."""
    args = [
        str(EDGE_EXE),
        f"--remote-debugging-port={DEBUG_PORT}",
        "--remote-allow-origins=*",  # CDP с конца 2023 требует явный allowlist
        f"--user-data-dir={USER_DATA_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]
    return subprocess.Popen(args, creationflags=subprocess.DETACHED_PROCESS if os.name == "nt" else 0)


def wait_for_debug_port(timeout: float = 15) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json/version", timeout=1)
            return True
        except Exception:
            time.sleep(0.3)
    return False


def get_cookies_via_cdp() -> list:
    """Подключаемся к CDP, получаем все куки."""
    targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json").read())
    if not targets:
        # Нет открытых таб — открываем blank
        urllib.request.urlopen(
            f"http://127.0.0.1:{DEBUG_PORT}/json/new?about:blank"
        ).read()
        time.sleep(0.5)
        targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json").read())

    target = next((t for t in targets if t.get("type") == "page"), targets[0])
    ws_url = target["webSocketDebuggerUrl"]

    ws = websocket.create_connection(ws_url, timeout=15)
    try:
        ws.send(json.dumps({"id": 1, "method": "Network.getAllCookies"}))
        # CDP отвечает событиями + ответом, выбираем по id
        deadline = time.time() + 20
        while time.time() < deadline:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                if "error" in msg:
                    raise RuntimeError(f"CDP error: {msg['error']}")
                return msg["result"]["cookies"]
        raise RuntimeError("CDP timeout — нет ответа на Network.getAllCookies")
    finally:
        ws.close()


def main() -> int:
    if not EDGE_EXE.exists():
        print(f"ERROR: Edge не найден: {EDGE_EXE}")
        return 2

    print("Извлечение Mi cookies через Edge CDP")
    print(f"  Edge: {EDGE_EXE}")
    print(f"  User data: {USER_DATA_DIR}")

    print("\n[1/4] Прибиваю текущий Edge (5с)...")
    kill_edge()

    print(f"[2/4] Запускаю Edge с --remote-debugging-port={DEBUG_PORT}...")
    edge_proc = start_edge_debug()

    print("[3/4] Жду готовности debug-порта...")
    if not wait_for_debug_port():
        print("ERROR: Edge не поднял debug-порт за 15с")
        return 1
    print("  OK, debug API доступно")

    print("[4/4] Запрашиваю куки через CDP...")
    try:
        cookies = get_cookies_via_cdp()
    except Exception as e:
        print(f"ERROR: {e}")
        kill_edge()
        return 1

    mi_cookies = [
        c for c in cookies
        if "mi.com" in c["domain"] or "xiaomi.com" in c["domain"]
    ]
    print(f"  Получено всего: {len(cookies)} cookies, Mi-related: {len(mi_cookies)}")

    found = {}
    for c in mi_cookies:
        if c["name"] in INTERESTING:
            v = c["value"]
            print(f"    [{c['domain']}] {c['name']}: len={len(v)} preview={v[:25]}...")
            if c["name"] not in found or len(v) > len(found[c["name"]]["value"]):
                found[c["name"]] = c

    OUTPUT.write_text(
        json.dumps(
            {
                "cookies": [{"host": c["domain"], "name": c["name"], "value": c["value"]} for c in mi_cookies],
                "key": {k: v["value"] for k, v in found.items()},
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nСохранено в {OUTPUT}")

    print("\nПрибиваю debug-Edge...")
    kill_edge()
    print("Готово. Можешь открывать Edge как обычно.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
