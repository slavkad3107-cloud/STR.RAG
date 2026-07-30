"""Синхронизация ключей API между компьютерами через папку переноса.

Просьба пользователя: «добавь ключи в .env в OneDrive, чтобы при копировании
базы из OneDrive и обратно они сами обновлялись».

Как работает:
  • .env лежит в каталоге данных, поэтому «📤 Выгрузить базу» кладёт его в
    папку переноса, а «📥 Забрать базу» приносит обратно — это штатный путь;
  • ДОПОЛНИТЕЛЬНО при запуске приложения: если .env в папке переноса СВЕЖЕЕ
    локального, ключи оттуда подмешиваются в локальный (значения из облака
    побеждают, локальные переменные, которых нет в облаке, сохраняются).
    Так второй компьютер получает исправленные ключи без полного переноса базы.

Секреты никуда не отправляются: только копирование между своими папками.
"""
from __future__ import annotations

from pathlib import Path

from ..paths import data_root

# переносим только ключи/настройки провайдеров — пути и служебное не трогаем
_SYNC_PREFIXES = ("DEEPSEEK", "OPENAI", "GEMINI", "GOOGLE", "ANTHROPIC", "MOONSHOT",
                  "KIMI", "MISTRAL", "CEREBRAS", "CELEBR", "GROQ", "OPENROUTER",
                  "COHERE", "HF_TOKEN", "OLLAMA")


def _parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _syncable(name: str) -> bool:
    return any(name.upper().startswith(p) for p in _SYNC_PREFIXES)


def sync_keys_from_transfer(dest: str | None = None) -> list[str]:
    """Подмешать ключи из <папка переноса>/.env в локальный .env.
    Возвращает список изменившихся переменных (пусто — ничего не делали)."""
    from .transfer import default_dest
    dest = dest or default_dest()
    if not dest:
        return []
    cloud = Path(dest) / ".env"
    local = data_root() / ".env"
    try:
        if not cloud.exists():
            return []
        if local.exists() and local.stat().st_mtime >= cloud.stat().st_mtime:
            return []  # локальный не старее — не трогаем
        cloud_vars = _parse_env(cloud.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []
    if not cloud_vars:
        return []

    try:
        local_text = local.read_text(encoding="utf-8") if local.exists() else ""
    except OSError:
        local_text = ""
    local_vars = _parse_env(local_text)

    changed: list[str] = []
    lines = local_text.splitlines()
    seen: set[str] = set()
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k = s.split("=", 1)[0].strip()
        seen.add(k)
        if not _syncable(k) or k not in cloud_vars:
            continue
        new = cloud_vars[k]
        if new and new != local_vars.get(k, ""):
            lines[i] = f"{k}={new}"
            changed.append(k)
    # переменные, которых в локальном файле нет вовсе
    add = [f"{k}={v}" for k, v in cloud_vars.items()
           if _syncable(k) and v and k not in seen]
    if add:
        lines += ["", "# --- добавлено синхронизацией ключей из папки переноса ---"] + add
        changed += [a.split("=", 1)[0] for a in add]

    if not changed:
        return []
    try:
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        return []
    # обновляем и текущий процесс, иначе ключи подхватятся только после перезапуска
    import os
    for k in changed:
        v = cloud_vars.get(k, "")
        if v:
            os.environ[k] = v
    return changed


def push_keys_to_transfer(dest: str | None = None) -> bool:
    """Положить локальный .env в папку переноса (чтобы второй компьютер забрал).
    Полный перенос базы делает это сам; функция нужна для кнопки «обновить ключи»."""
    from .transfer import default_dest
    dest = dest or default_dest()
    if not dest:
        return False
    src = data_root() / ".env"
    dst = Path(dest) / ".env"
    try:
        if not src.exists():
            return False
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        return True
    except OSError:
        return False
