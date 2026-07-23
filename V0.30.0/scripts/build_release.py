# -*- coding: utf-8 -*-
"""Собрать нумерованный релиз STR.RAG в releases/STR.RAG-v{версия}/ (+ .zip).

Как в ЭКО.DOC: каждая версия — ОТДЕЛЬНАЯ ПАПКА С НОМЕРОМ в имени
(releases/STR.RAG-vX.Y.Z), из которой и запускают приложение — так всегда видно,
какая версия у тебя. Рядом кладётся такой же .zip (для переноса/архива).
Версия берётся из pmoos/__init__.__version__ — имя всегда совпадает с кодом.

venv НЕ копируется: он общий (%USERPROFILE%\\.pmoos-rag\\venv) и run.bat находит
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
    # релизы кладём НА УРОВЕНЬ ВЫШE папки приложения (рядом с ней), как в ЭКО.DOC:
    # <репозиторий>/releases/STR.RAG-vX.Y.Z/ — так они на виду, а не спрятаны
    # внутри рабочей папки.
    rel_dir = APP.parent / "releases"
    rel_dir.mkdir(exist_ok=True)
    top = f"STR.RAG-v{__version__}"
    folder = rel_dir / top                 # РАСПАКОВАННАЯ папка релиза (запускать из неё)
    zip_out = rel_dir / f"{top}.zip"        # zip рядом (перенос/архив)

    # 1) собрать файлы релиза (без мусора/venv/ключей)
    files = [p for p in sorted(APP.rglob("*"))
             if p.is_file() and _keep(p.relative_to(APP))]

    # 2) распакованная папка: чистим прошлую сборку этой версии и копируем заново
    if folder.exists():
        shutil.rmtree(folder)
    for p in files:
        rel = p.relative_to(APP)
        dst = folder / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)

    # 3) zip рядом (та же структура, верхняя папка = имя версии)
    with zipfile.ZipFile(zip_out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p, f"{top}/{p.relative_to(APP).as_posix()}")

    size_mb = zip_out.stat().st_size / 2**20
    print(f"Собран релиз v{__version__} «{__codename__}» ({len(files)} файлов):")
    print(f"  папка (запускать отсюда): {folder}\\run.bat")
    print(f"  архив (перенос):          {zip_out.name}  ({size_mb:.1f} МБ)")
    return folder


if __name__ == "__main__":
    build()
