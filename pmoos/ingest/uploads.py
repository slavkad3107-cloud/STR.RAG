"""Приём файлов проекта: сохранение, распаковка zip, копирование из папки.

Общий слой для GUI-сервера и CLI (архитектура как в ЭКО.DOC: тяжёлая работа —
обычные Python-модули, интерфейс — тонкая обёртка).
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from ..paths import project_paths

_BAD = re.compile(r'[<>:"|?*]')


def _flat(parts: list[str]) -> str:
    """Сплющить путь в имя «подпапка__файл.pdf» (одноимённые файлы из разных
    подпапок не затирают друг друга) и вычистить запрещённые символы Windows."""
    return _BAD.sub("_", "__".join(p for p in parts if p))


def save_bytes(project: str, name: str, data: bytes) -> list[str]:
    """Сохранить один файл (или распаковать zip). Возвращает список имён."""
    from .loaders import SUPPORTED_EXT
    up = project_paths(project)["uploads"]
    up.mkdir(parents=True, exist_ok=True)
    name = Path(str(name).replace("\\", "/")).name
    if name.lower().endswith(".zip"):
        import io
        import zipfile
        saved: list[str] = []
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for zi in zf.infolist():
                if zi.is_dir() or Path(zi.filename).suffix.lower() not in SUPPORTED_EXT:
                    continue
                if zi.file_size > 1_500_000_000:      # защита от zip-бомбы
                    continue
                try:
                    raw = zi.filename.encode("cp437").decode("cp866")
                except Exception:  # noqa: BLE001 — не-русский zip
                    raw = zi.filename
                parts = raw.replace("\\", "/").strip("/").split("/")
                if "__MACOSX" in parts or parts[-1].startswith("._"):
                    continue
                flat = _flat(parts)
                if not flat:
                    continue
                with zf.open(zi) as src, open(up / flat, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                saved.append(flat)
        return saved
    if Path(name).suffix.lower() not in SUPPORTED_EXT:
        raise ValueError(f"формат «{Path(name).suffix}» не поддерживается")
    (up / _BAD.sub("_", name)).write_bytes(data)
    return [name]


def unpack_zip_path(project: str, zip_path: str | Path) -> list[str]:
    """Распаковать zip С ДИСКА без чтения архива в память (ревью: скачанный по
    ссылке том >1 ГБ читался целиком в RAM дважды)."""
    from .loaders import SUPPORTED_EXT
    up = project_paths(project)["uploads"]
    up.mkdir(parents=True, exist_ok=True)
    import zipfile
    saved: list[str] = []
    with zipfile.ZipFile(str(zip_path)) as zf:
        for zi in zf.infolist():
            if zi.is_dir() or Path(zi.filename).suffix.lower() not in SUPPORTED_EXT:
                continue
            if zi.file_size > 1_500_000_000:
                continue
            try:
                raw = zi.filename.encode("cp437").decode("cp866")
            except Exception:  # noqa: BLE001
                raw = zi.filename
            parts = raw.replace("\\", "/").strip("/").split("/")
            if "__MACOSX" in parts or parts[-1].startswith("._"):
                continue
            flat = _flat(parts)
            if not flat:
                continue
            with zf.open(zi) as src, open(up / flat, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            saved.append(flat)
    return saved


def copy_folder(project: str, folder: str | Path) -> int:
    """Скопировать поддерживаемые файлы из папки (с подпапками) в проект."""
    from .loaders import SUPPORTED_EXT
    src = Path(str(folder).strip().strip('"'))
    if not src.is_dir():
        raise FileNotFoundError(f"папка не найдена: {src}")
    up = project_paths(project)["uploads"]
    up.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in sorted(src.rglob("*")):
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXT:
            try:
                shutil.copy2(f, up / _flat(list(f.relative_to(src).parts)))
                n += 1
            except OSError:
                continue
    return n


def list_uploads(project: str) -> list[dict]:
    up = project_paths(project)["uploads"]
    if not up.exists():
        return []
    return [{"name": p.name, "kb": p.stat().st_size // 1024}
            for p in sorted(up.iterdir()) if p.is_file()]
