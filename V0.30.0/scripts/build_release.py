# -*- coding: utf-8 -*-
"""Собрать нумерованный релиз STR.RAG в releases/STR.RAG-v{версия}.zip.

Как в ЭКО.DOC: каждая версия — отдельный zip с номером в имени, чтобы всегда
было видно, какая версия у тебя и куда обновляться. Версия берётся из
pmoos/__init__.__version__ — имя файла всегда совпадает с кодом внутри.

Запуск:  python scripts/build_release.py         (из папки приложения)
   либо:  собрать_релиз.bat                       (двойным кликом)

В архив НЕ кладём мусор и приватное: __pycache__, .git, venv, кэши, ключи.
"""
from __future__ import annotations

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
    # <репозиторий>/releases/STR.RAG-vX.Y.Z.zip — так они на виду, а не спрятаны
    # внутри рабочей папки.
    rel_dir = APP.parent / "releases"
    rel_dir.mkdir(exist_ok=True)
    out = rel_dir / f"STR.RAG-v{__version__}.zip"
    top = f"STR.RAG-v{__version__}"        # верхняя папка внутри архива
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(APP.rglob("*")):
            if not p.is_file() or not _keep(p.relative_to(APP)):
                continue
            z.write(p, f"{top}/{p.relative_to(APP).as_posix()}")
            n += 1
    size_mb = out.stat().st_size / 2**20
    print(f"Собран релиз: {out.name}  ({n} файлов, {size_mb:.1f} МБ, "
          f"v{__version__} «{__codename__}»)")
    print(f"Путь: {out}")
    return out


if __name__ == "__main__":
    build()
