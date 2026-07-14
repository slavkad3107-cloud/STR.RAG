"""Работа с локальным Ollama.

Замечание пользователя: «предлагать в списке все локальные модели через
провайдера ollama, найденные и установленные». Плюс жалоба: Ollama запущена,
но приложение её «не видит». Поэтому детект сделан устойчивым:
  • учитывается переменная окружения OLLAMA_HOST;
  • пробуем несколько адресов (localhost / 127.0.0.1) и эндпоинтов (/api/tags,
    /api/version), с увеличенным таймаутом;
  • запасной путь — CLI `ollama list`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time

# TTL-кэш проб (15 с): Streamlit ререндерит ВСЕ вкладки на каждый клик, и при
# провайдере ollama выходило до 4× живых HTTP-проб (timeout 6-8 с) на клик —
# многосекундный фриз интерфейса. Свежесть `ollama pull` с задержкой ≤15 с
# некритична; генерация ответов идёт напрямую, мимо этих проб.
_PROBE_TTL = 15.0
_PROBE_CACHE: dict[tuple, tuple[float, object]] = {}


def _cached(key: tuple, compute):
    now = time.monotonic()
    hit = _PROBE_CACHE.get(key)
    if hit and now - hit[0] < _PROBE_TTL:
        return hit[1]
    val = compute()
    _PROBE_CACHE[key] = (now, val)
    return val


def _candidate_urls(base_url: str | None = None) -> list[str]:
    urls: list[str] = []
    for u in (base_url, os.environ.get("OLLAMA_HOST"),
              "http://localhost:11434", "http://127.0.0.1:11434"):
        if not u:
            continue
        v = u.strip()
        if not v.startswith("http"):
            v = "http://" + v            # OLLAMA_HOST может быть как 'localhost:11434'
        v = v.rstrip("/")
        if v not in urls:
            urls.append(v)
    return urls


def ollama_base_url(base_url: str | None = None) -> str:
    """Первый рабочий адрес Ollama (или localhost по умолчанию)."""
    import requests
    for u in _candidate_urls(base_url):
        for ep in ("/api/tags", "/api/version"):
            try:
                r = requests.get(u + ep, timeout=6)
                if r.status_code == 200:
                    return u
            except Exception:
                continue
    return _candidate_urls(base_url)[0]


def ollama_available(base_url: str | None = None) -> bool:
    return _cached(("avail", base_url), lambda: _ollama_available_raw(base_url))


def _ollama_available_raw(base_url: str | None = None) -> bool:
    import requests
    for u in _candidate_urls(base_url):
        for ep in ("/api/tags", "/api/version"):
            try:
                r = requests.get(u + ep, timeout=6)
                if r.status_code == 200:
                    return True
            except Exception:
                continue
    # запасной вариант: бинарь установлен (служба могла не успеть подняться)
    return shutil.which("ollama") is not None


def list_installed_models(base_url: str | None = None) -> list[str]:
    """Список установленных моделей (TTL-кэш 15 с). Сначала HTTP API, затем CLI."""
    return _cached(("models", base_url), lambda: _list_installed_models_raw(base_url))


def _list_installed_models_raw(base_url: str | None = None) -> list[str]:
    import requests
    for u in _candidate_urls(base_url):
        try:
            r = requests.get(u + "/api/tags", timeout=8)
            if r.status_code == 200:
                data = r.json() or {}
                names = [m.get("name", "") for m in data.get("models", [])]
                names = [n for n in names if n]
                if names:
                    return sorted(set(names))
        except Exception:
            continue
    # CLI fallback
    try:
        out = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=10)
        names = []
        for line in out.stdout.splitlines()[1:]:
            parts = line.split()
            if parts:
                names.append(parts[0])
        return sorted(set(n for n in names if n))
    except Exception:
        return []
