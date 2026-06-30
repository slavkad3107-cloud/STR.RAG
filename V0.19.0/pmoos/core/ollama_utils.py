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
    """Список установленных моделей. Сначала через HTTP API (по всем адресам),
    затем через CLI `ollama list`."""
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
