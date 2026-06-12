"""
Mi Cloud клиент через cookie-based авторизацию (без captcha).

Использует passToken из браузерных кук для получения свежего ssecurity
и serviceToken под нужный sid (xiaomiio для Mi Home / mishealth для health-данных).
Затем делает signed API запросы к https://api.io.mi.com/app/...

Применяется когда обычный password-login заблокирован каптчей Xiaomi.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path

import requests


# ===== RC4 (с DROP1024) для шифрования payload в Mi Cloud =====

class _RC4:
    """RC4 stream cipher с режимом DROP1024 (стандарт Mi Cloud)."""
    def __init__(self, key: bytes):
        S = list(range(256))
        j = 0
        for i in range(256):
            j = (j + S[i] + key[i % len(key)]) & 0xff
            S[i], S[j] = S[j], S[i]
        self.S = S
        self.i = 0
        self.j = 0
        # DROP1024: пропускаем первые 1024 байта keystream
        self.crypt(b"\x00" * 1024)

    def crypt(self, data: bytes) -> bytes:
        S, i, j = self.S, self.i, self.j
        out = bytearray()
        for c in data:
            i = (i + 1) & 0xff
            j = (j + S[i]) & 0xff
            S[i], S[j] = S[j], S[i]
            out.append(c ^ S[(S[i] + S[j]) & 0xff])
        self.i, self.j = i, j
        return bytes(out)


def _rc4_encrypt(key_b64: str, data: str) -> str:
    """key_b64 = base64 ключ (signed_nonce); вернёт base64 шифротекста."""
    return base64.b64encode(_RC4(base64.b64decode(key_b64)).crypt(data.encode())).decode()


def _rc4_decrypt(key_b64: str, data_b64: str) -> str:
    """RC4 симметричный, decrypt = encrypt."""
    return _RC4(base64.b64decode(key_b64)).crypt(base64.b64decode(data_b64)).decode("utf-8", errors="replace")

BASE = Path(__file__).parent
COOKIES_PATH = BASE / "mi_cookies.json"


def _load_cookies() -> dict:
    if not COOKIES_PATH.exists():
        raise RuntimeError(
            f"Нет {COOKIES_PATH}. Запусти extract_mi_cookies.py пока Edge закрыт."
        )
    data = json.loads(COOKIES_PATH.read_text(encoding="utf-8"))
    return data.get("key", {})


class MiCloudCookieClient:
    """Mi Cloud client using passToken cookie (skips password login + captcha)."""

    def __init__(self, region: str = "cn"):
        self.region = region
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "APP/com.xiaomi.mihome APPV/10.0.10 iosPassportSDK/3.9.0 iOS/16.0 miHSTS",
        })
        self.user_id: str | None = None
        self.cuser_id: str | None = None
        self.pass_token: str | None = None
        self.service_token: str | None = None
        self.ssecurity: str | None = None
        self.device_id = uuid.uuid4().hex[:16].upper()

    @property
    def api_base(self) -> str:
        prefix = "" if self.region == "cn" else f"{self.region}."
        return f"https://{prefix}api.io.mi.com/app"

    def login_via_pass_token(self):
        """Login flow without password — uses passToken from extracted cookies."""
        creds = _load_cookies()
        self.user_id = creds.get("userId")
        self.cuser_id = creds.get("cUserId")
        self.pass_token = creds.get("passToken")
        if not (self.user_id and self.pass_token):
            raise RuntimeError(
                "В mi_cookies.json нет userId или passToken — куки не вытащились корректно."
            )

        # Set passToken cookie so /pass/serviceLogin recognizes us
        self.session.cookies.set("userId", self.user_id, domain=".xiaomi.com")
        self.session.cookies.set("passToken", self.pass_token, domain=".xiaomi.com")
        if self.cuser_id:
            self.session.cookies.set("cUserId", self.cuser_id, domain=".xiaomi.com")
        self.session.cookies.set("deviceId", self.device_id, domain=".xiaomi.com")

        # Step 1: GET /pass/serviceLogin?sid=xiaomiio — server sees passToken → returns location with code
        r1 = self.session.get(
            "https://account.xiaomi.com/pass/serviceLogin",
            params={"sid": "xiaomiio", "_json": "true"},
            timeout=15,
        )
        body = r1.text.replace("&&&START&&&", "")
        try:
            j1 = json.loads(body)
        except Exception:
            raise RuntimeError(f"step1 невалидный JSON: {body[:200]}")

        # If location is full URL, passToken auth succeeded; if not we need password (shouldn't happen with valid passToken)
        location = j1.get("location")
        result = j1.get("result")
        if not location or result != "ok":
            code = j1.get("code")
            desc = j1.get("description", "")
            raise RuntimeError(f"step1 не зашло: result={result} code={code} desc={desc}")

        # Capture user data from response
        self.ssecurity = j1.get("ssecurity")
        if j1.get("userId"):
            self.user_id = str(j1["userId"])
        if j1.get("cUserId"):
            self.cuser_id = j1["cUserId"]

        # Step 3: GET location to set serviceToken cookie
        r3 = self.session.get(location, timeout=15, allow_redirects=False)
        if r3.status_code not in (200, 302):
            raise RuntimeError(f"step3 status={r3.status_code}: {r3.text[:200]}")

        # serviceToken in cookies after redirect
        for c in self.session.cookies:
            if c.name == "serviceToken" and (".io.mi.com" in c.domain or "mi.com" in c.domain):
                self.service_token = c.value
                break

        if not self.service_token or not self.ssecurity:
            raise RuntimeError(
                f"После step3 нет serviceToken/ssecurity: token={bool(self.service_token)} ssec={bool(self.ssecurity)}"
            )

        return True

    # ===== Signed API requests =====

    def _signed_nonce(self, nonce: str) -> str:
        """ssecurity + nonce → SHA256 → base64 (signing key)."""
        m = hashlib.sha256()
        m.update(base64.b64decode(self.ssecurity))
        m.update(base64.b64decode(nonce))
        return base64.b64encode(m.digest()).decode()

    @staticmethod
    def _generate_nonce() -> str:
        rand = os.urandom(8)
        ts_min = int(time.time() / 60).to_bytes(4, "big")
        return base64.b64encode(rand + ts_min).decode()

    def _gen_enc_signature(self, method: str, url: str, signed_nonce: str, params: dict) -> str:
        """SHA1 sign: method + path(without /app/) + params (insertion order!) + signed_nonce."""
        # Mi Cloud REPLACES /app/ with /, не sorted ключи
        path = url.split(".com")[1].replace("/app/", "/")
        parts = [method.upper(), path]
        for k, v in params.items():
            parts.append(f"{k}={v}")
        parts.append(signed_nonce)
        sign_string = "&".join(parts)
        return base64.b64encode(hashlib.sha1(sign_string.encode()).digest()).decode()

    def request(self, path: str, payload: dict | None = None, method: str = "POST") -> dict:
        if not self.service_token or not self.ssecurity:
            raise RuntimeError("Не залогинен. Сначала вызови login_via_pass_token().")

        url = self.api_base + path
        nonce = self._generate_nonce()
        signed_nonce = self._signed_nonce(nonce)

        # Точная имплементация Mi Cloud RC4-flow (по Xiaomi-cloud-tokens-extractor):
        # 1) params в нужном порядке: data сначала
        params = {"data": json.dumps(payload or {}, separators=(",", ":"))}
        # 2) rc4_hash__ = SHA1 от плейн-параметров
        params["rc4_hash__"] = self._gen_enc_signature(method, url, signed_nonce, params)
        # 3) Шифруем ВСЕ value RC4
        for k in params:
            params[k] = _rc4_encrypt(signed_nonce, params[k])
        # 4) Финальная подпись от ЗАШИФРОВАННЫХ параметров
        params["signature"] = self._gen_enc_signature(method, url, signed_nonce, params)
        params["_nonce"] = nonce
        params["ssecurity"] = self.ssecurity

        cookies = {
            "userId": str(self.user_id),
            "yetAnotherServiceToken": self.service_token,
            "serviceToken": self.service_token,
            "locale": "en_GB",
            "timezone": "GMT+02:00",
            "is_daylight": "1",
            "dst_offset": "3600000",
            "channel": "MI_APP_STORE",
        }
        headers = {
            "X-XIAOMI-PROTOCAL-FLAG-CLI": "PROTOCAL-HTTP2",
            "MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4",
            "Accept-Encoding": "identity",
            "User-Agent": "Android-7.1.1-1.0.0-ONEPLUS A3010-136-AndroidIotPushSDK/3.4.1",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        # ВАЖНО: params= (URL query), не data= (form body) — так делает референсная имплементация
        r = self.session.post(url, params=params, cookies=cookies, headers=headers, timeout=20)

        text = r.text
        # Ответ тоже зашифрован RC4
        try:
            decrypted = _rc4_decrypt(signed_nonce, text)
            return json.loads(decrypted)
        except Exception:
            try:
                return json.loads(text)
            except Exception:
                raise RuntimeError(f"Non-JSON response (status {r.status_code}): {text[:300]}")

    # ===== High-level helpers =====

    def get_devices(self) -> list:
        """Список всех устройств в Mi Home."""
        resp = self.request("/home/device_list", {"getVirtualModel": False, "getHuamiDevices": 1})
        return (resp.get("result") or {}).get("list") or []

    def get_device_events(self, did: str, limit: int = 50) -> list:
        """События устройства (для T700 — это сессии чистки)."""
        resp = self.request("/v2/device/get_event", {"did": did, "limit": limit, "ascending": False})
        return (resp.get("result") or {}).get("event_list") or []


def quick_test():
    """Быстрый тест: логин + список устройств."""
    client = MiCloudCookieClient(region="cn")
    print("Login через passToken...")
    client.login_via_pass_token()
    print(f"  user_id={client.user_id}")
    print(f"  ssecurity len={len(client.ssecurity or '')}")
    print(f"  serviceToken len={len(client.service_token or '')}")

    print("\nGet devices...")
    devices = client.get_devices()
    print(f"  Found {len(devices)} devices")
    for d in devices:
        name = d.get("name", "?")
        model = d.get("model", "?")
        did = d.get("did", "?")
        online = d.get("isOnline", False)
        print(f"    [{did}] {name} ({model}) online={online}")
    return client, devices


if __name__ == "__main__":
    quick_test()
