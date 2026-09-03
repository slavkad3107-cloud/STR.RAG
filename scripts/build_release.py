# -*- coding: utf-8 -*-
"""Собрать нумерованный релиз STR.RAG в releases/STR.RAG-v{версия}/ (+ .zip).

Как в ЭКО.DOC: каждая версия — ОТДЕЛЬНАЯ ПАПКА С НОМЕРОМ в имени
(releases/STR.RAG-vX.Y.Z), из которой и запускают приложение — так всегда видно,
какая версия у тебя. Рядом кладётся такой же .zip (для переноса/архива).
Версия берётся из pmoos/__init__.__version__ — имя всегда совпадает с кодом.

venv НЕ копируется: он общий (%USERPROFILE%\\.pmoos-rag\\venv) и СТРОЙРАГ.bat находит
его сам — поэтому папка релиза лёгкая (только код).

Запуск:  python scripts/build_release.py         (из папки приложения)
   либо:  собрать_релиз.bat                       (двойным кликом)

В релиз НЕ кладём мусор и приватное: __pycache__, .git, venv, кэши, ключи.
"""
from __future__ import annotations

import shutil
import sys
import zipfile
from pathlib import Path

APP = Path(__file__).resolve().parent.parent   # папка приложения (где pmoos/)
sys.path.insert(0, str(APP))
from pmoos import __version__, __codename__     # noqa: E402

# каталоги/файлы, которые в релиз НЕ попадают
SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "pip-cache", "releases",
             ".pytest_cache", ".ruff_cache", "models"}
SKIP_EXT = {".pyc", ".pyo", ".log", ".zip"}
SKIP_NAMES = {".env"}  # ключи не распространяем (в релизе только .env.example)


def _keep(p: Path) -> bool:
    if any(part in SKIP_DIRS for part in p.parts):
        return False
    if p.suffix.lower() in SKIP_EXT or p.name in SKIP_NAMES:
        return False
    return True


def build() -> Path:
    # Структура с v0.34: ИСХОДНЫЙ КОД живёт отдельно (папка разработки), а
    # витрина STR.RAG содержит только релизы и резюме — как просил пользователь.
    # Собираем из папки с кодом → в <...>/STR.RAG/releases/STR.RAG-vX.Y.Z/.
    # Витрину ищем по СОДЕРЖИМОМУ, а не по точному имени: папку переименовывали
    # (STR.RAG → STRRAG), и сборка молча уходила внутрь папки разработки.
    def _find_showcase() -> Path | None:
        import os
        env = os.environ.get("STRRAG_RELEASES_DIR", "").strip()
        if env:
            return Path(env).parent if Path(env).name == "releases" else Path(env)
        cands = []
        for d in sorted(APP.parent.iterdir()):
            if not d.is_dir() or d.resolve() == APP.resolve() or d.name.startswith("_"):
                continue
            if (d / "releases").is_dir() or any(d.glob("РЕЗЮМЕ_*.md")):
                cands.append(d)
        return cands[0] if cands else None

    showcase = _find_showcase()
    rel_dir = ((showcase / "releases") if showcase else (APP / "releases"))
    rel_dir.mkdir(parents=True, exist_ok=True)
    top = f"STR.RAG-v{__version__}"
    folder = rel_dir / top                 # РАСПАКОВАННАЯ папка релиза (запускать из неё)
    zip_out = rel_dir / f"{top}.zip"        # zip рядом (перенос/архив)

    # 1) собрать файлы релиза (без мусора/venv/ключей)
    files = [p for p in sorted(APP.rglob("*"))
             if p.is_file() and _keep(p.relative_to(APP))]

    # 2) распакованная папка. НЕ сносим её целиком: OneDrive держит файлы, и
    #    rmtree падал PermissionError ПОСРЕДИ удаления — оставался БИТЫЙ релиз
    #    (пропадал app/hub.py, приложение не запускалось). Теперь копируем
    #    поверх с повторами, а лишнее удаляем по одному файлу.
    import time as _t
    expected: set[Path] = set()
    failed: list[str] = []
    for p in files:
        rel = p.relative_to(APP)
        expected.add(rel)
        dst = folder / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(4):
            try:
                shutil.copy2(p, dst)
                break
            except (PermissionError, OSError):
                _t.sleep(0.4)          # файл занят синхронизацией — подождём
        else:
            failed.append(str(rel))
    if folder.exists():                 # снести устаревшее из прошлой сборки
        for old in sorted(folder.rglob("*"), reverse=True):
            try:
                rel_old = old.relative_to(folder)
                if old.is_file() and rel_old not in expected:
                    old.unlink()
                elif old.is_dir() and not any(old.iterdir()):
                    old.rmdir()
            except OSError:
                pass                    # занято — не критично, перезапишется

    # 3) ПРОВЕРКА ЦЕЛОСТНОСТИ: релиз без этих файлов не запустится
    must = ["app/gui/server.py", "app/gui/index.html", "pmoos/__init__.py",
            "СТРОЙРАГ.bat"]
    missing = [m for m in must if not (folder / m).exists()]
    if missing or failed:
        print("ОШИБКА СБОРКИ: релиз НЕПОЛНЫЙ.")
        if missing:
            print("  нет обязательных файлов:", ", ".join(missing))
        if failed:
            print("  не скопировались (заняты):", ", ".join(failed[:8]))
        print("  Закройте приложение/подождите синхронизации OneDrive и повторите.")
        raise SystemExit(2)

    # 3) zip рядом (та же структура, верхняя папка = имя версии)
    with zipfile.ZipFile(zip_out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p, f"{top}/{p.relative_to(APP).as_posix()}")

    size_mb = zip_out.stat().st_size / 2**20
    print(f"Собран релиз v{__version__} «{__codename__}» ({len(files)} файлов):")
    print(f"  папка (запускать отсюда): {folder}\\СТРОЙРАГ.bat")
    print(f"  архив (перенос):          {zip_out.name}  ({size_mb:.1f} МБ)")
    return folder


if __name__ == "__main__":
    build()
