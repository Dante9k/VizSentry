"""Local configuration. Secrets are encrypted with Windows DPAPI at rest."""
from __future__ import annotations

import base64
import copy
import ctypes
from ctypes import wintypes
import json
import os
import threading
from pathlib import Path

DEFAULTS = {
    "ai": {"base_url": "", "model": "", "criteria": "", "api_key": ""},
    "feishu": {"app_id": "", "app_secret": ""},
    "concurrency": {"download": 4, "ai": 2, "browser": 2},
    "log_sites": [],
}


def default_data_dir() -> Path:
    if value := os.environ.get("VIZSENTRY_DATA_DIR"):
        return Path(value).resolve()
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share")))
    return base / "VizSentry"


def _dpapi(data: bytes, decrypt: bool = False) -> bytes:
    class Blob(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    library = ctypes.WinDLL('crypt32', use_last_error=True)
    library.CryptProtectData.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    library.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    library.CryptProtectData.restype = library.CryptUnprotectData.restype = wintypes.BOOL
    if decrypt:
        ok = library.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        ok = library.CryptProtectData(ctypes.byref(source), "VizSentry", None, None, None, 1, ctypes.byref(output))
    if not ok:
        raise OSError(ctypes.get_last_error(), '无法使用 Windows 凭证加密')
    try:
        return ctypes.string_at(output.data, output.length)
    finally:
        release = ctypes.windll.kernel32.LocalFree
        release.argtypes = [ctypes.c_void_p]
        release.restype = ctypes.c_void_p
        release(output.data)


class SettingsStore:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "settings.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def _decode(self, value: str) -> str:
        if value.startswith("dpapi:"):
            return _dpapi(base64.b64decode(value[6:]), True).decode()
        return value

    def _encode(self, value: str) -> str:
        if os.name == "nt" and value:
            return "dpapi:" + base64.b64encode(_dpapi(value.encode())).decode()
        return value

    def read(self, masked: bool = False) -> dict:
        with self.lock:
            config = copy.deepcopy(DEFAULTS)
            if self.path.exists():
                saved = json.loads(self.path.read_text("utf-8"))
                for section in ("ai", "feishu", "concurrency"):
                    config[section].update(saved.get(section, {}))
                config["log_sites"] = saved.get("log_sites", [])
            for section, name in (("ai", "api_key"), ("feishu", "app_secret")):
                value = self._decode(config[section].get(name, ""))
                config[section][name] = "" if masked else value
                if masked:
                    config[section][name + "_configured"] = bool(value)
            for site in config["log_sites"]:
                value = self._decode(site.get("token", ""))
                site["token"] = "" if masked else value
                if masked:
                    site["token_configured"] = bool(value)
            return config

    def update(self, changes: dict) -> dict:
        with self.lock:
            config = self.read()
            for section in ("ai", "feishu", "concurrency"):
                for key, value in changes.get(section, {}).items():
                    if key.endswith("_configured"):
                        continue
                    if key in ("api_key", "app_secret") and not value:
                        continue
                    config[section][key] = value
            for key in ("download", "ai", "browser"):
                config["concurrency"][key] = max(1, min(16, int(config["concurrency"][key])))
            if "log_sites" in changes:
                previous = {site.get("domain"): site for site in config["log_sites"]}
                config["log_sites"] = []
                for site in changes["log_sites"]:
                    site = dict(site)
                    if not site.get("token"):
                        site["token"] = previous.get(site.get("domain"), {}).get("token", "")
                    site.pop("token_configured", None)
                    config["log_sites"].append(site)
            for section, key in (("ai", "api_key"), ("feishu", "app_secret")):
                config[section][key] = self._encode(config[section].get(key, ""))
            for site in config["log_sites"]:
                site["token"] = self._encode(site.get("token", ""))
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(config, ensure_ascii=False, indent=2), "utf-8")
            if os.name != "nt":
                temp.chmod(0o600)
            temp.replace(self.path)
            return self.read(masked=True)
