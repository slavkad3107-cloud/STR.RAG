"""Закрытие старых сеансов при старте приложения (просьба пользователя:
«сделать поиск и закрытие старых сеансов, если при запуске обнаруживает»).

Два уровня:
  1. Заглохшие состояния «running» (индексация / поиск ответов) с пропавшим
     пульсом — помечаются paused с понятным сообщением («Продолжить» возобновит).
  2. Прошлый экземпляр приложения: pid предыдущего запуска хранится в
     <данные>/session.pid; если он ещё жив, это python/streamlit И в его
     командной строке наш код — процесс закрывается (иначе он держал бы
     базу Qdrant, и поиск ответов падал бы «база занята»).

Никогда не трогаем процессы, которые не можем ОДНОЗНАЧНО опознать как свои.
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime

from ..paths import data_root

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _age_sec(ts: str) -> float:
    try:
        return max(0.0, (datetime.now() - datetime.fromisoformat(ts)).total_seconds())
    except (ValueError, TypeError):
        return 1e9


def _cmdline(pid: int) -> str:
    """Командная строка процесса ('' если мёртв/не узнать). wmic удалён из
    новых Windows 11 (ревью) — второй путь через PowerShell CIM."""
    attempts = (
        ["wmic", "process", "where", f"processid={pid}", "get", "commandline"],
        ["powershell", "-NoProfile", "-Command",
         f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
    )
    for cmd in attempts:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=15, creationflags=_NOWIN)
            out = (r.stdout or "").strip()
            if out:
                return out
        except Exception:  # noqa: BLE001
            continue
    return ""


def _looks_like_ours(cmd: str) -> bool:
    """УЗКОЕ опознание прошлого экземпляра ПРИЛОЖЕНИЯ (ревью: «python+pmoos»
    матчил и наши фоновые ВОРКЕРЫ — при переиспользовании pid убили бы живую
    индексацию/генерацию). С v0.43 главный процесс — stdlib-сервер
    app/gui/server.py; «streamlit …hub.py» оставлен для добивания сеансов,
    запущенных СТАРЫМИ версиями до обновления."""
    low = cmd.lower().replace("\\", "/")
    if "block1_answers" in low or "pmoos.index.indexer" in low:
        return False  # фоновые воркеры — не главный процесс, не трогаем
    if "app/gui/server.py" in low:
        return True
    return "streamlit" in low and ("hub.py" in low or "pmoos" in low
                                   or "str.rag" in low)


def close_stale_sessions() -> list[str]:
    """Вернуть список человекочитаемых заметок о том, что было закрыто."""
    notes: list[str] = []

    # 1) заглохшие running-состояния → paused (возобновляемо)
    try:
        from ..projects import list_projects
        from ..index import indexer as I
        from ..pipeline import block1_answers as B
        for prj in list_projects():
            for title, read_, write_ in (
                    ("индексация", I.read_state, I.write_state),
                    ("поиск ответов", B.read_answers_state, B.write_answers_state)):
                try:
                    stt = read_(prj)
                    if stt.get("status") != "running":
                        continue
                    if _age_sec(stt.get("heartbeat") or stt.get("updated_at") or "") <= 120:
                        continue  # живой процесс — не трогаем
                    stt.update({"status": "paused", "pid": 0, "pause_requested": False,
                                "message": "Прошлый сеанс закрыт при старте приложения "
                                           "(пульс пропал). «⏯ Продолжить» возобновит."})
                    write_(prj, stt)
                    notes.append(f"«{prj}»: прерванный сеанс ({title}) закрыт — "
                                 f"можно «⏯ Продолжить»")
                except Exception:  # noqa: BLE001
                    continue
    except Exception:  # noqa: BLE001
        pass

    # 2) прошлый экземпляр приложения (держал бы базу Qdrant)
    pf = data_root() / "session.pid"
    prev = 0
    try:
        prev = int(pf.read_text(encoding="ascii").strip()) if pf.exists() else 0
    except (OSError, ValueError):
        prev = 0
    cur = os.getpid()
    if prev and prev != cur:
        cmd = _cmdline(prev)
        if cmd and _looks_like_ours(cmd):
            try:
                subprocess.run(["taskkill", "/PID", str(prev), "/F"],
                               capture_output=True, timeout=10, creationflags=_NOWIN)
                notes.append(f"Закрыт старый сеанс приложения (pid {prev}) — "
                             f"он держал бы базу занятой")
            except Exception:  # noqa: BLE001
                pass
    try:
        pf.write_text(str(cur), encoding="ascii")
    except OSError:
        pass
    return notes
