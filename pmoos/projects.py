"""Реестр проектов: сопоставление человекочитаемого имени и каталога.

Имена проектов хранятся в data_dir/projects.json как {slug: original_name}.
Сами файлы ПД не сохраняются (требование пользователя) — в каталоге проекта
лежат только карта разделов, версии, граф, ответы и RAG-метаданные.
"""
from __future__ import annotations

import json

from .paths import projects_registry, slugify, data_root


def _read() -> dict[str, str]:
    p = projects_registry()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _write(data: dict[str, str]) -> None:
    projects_registry().write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def register_project(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return ""
    data = _read()
    data[slugify(name)] = name
    _write(data)
    return name


def list_projects() -> list[str]:
    """Имена проектов: из реестра + досканируем каталог projects/ (на всякий случай)."""
    data = _read()
    names = set(data.values())
    proj_root = data_root() / "projects"
    if proj_root.exists():
        known_slugs = set(data.keys())
        for d in proj_root.iterdir():
            if d.is_dir() and d.name not in known_slugs:
                # пробуем восстановить настоящее имя из inventory.json
                inv = d / "inventory.json"
                real = None
                if inv.exists():
                    try:
                        real = json.loads(inv.read_text(encoding="utf-8")).get("project")
                    except Exception:  # noqa: BLE001
                        real = None
                names.add(real or d.name)
    return sorted(names)


def forget_project(name: str) -> None:
    data = _read()
    data.pop(slugify(name), None)
    _write(data)


# ─────────── удалить / экспорт / импорт объекта (v0.48, ТЗ) ───────────
_SKIP_DIRS = {"tmp_uploads", "__pycache__"}


def _dir_nocreate(name: str):
    """Путь каталога проекта БЕЗ создания (paths.project_dir создаёт папку —
    из-за этого проверка «существует ли» всегда была истинной)."""
    return data_root() / "projects" / slugify(name)


def delete_project(name: str) -> dict:
    """«Удалить объект»: папка проекта переносится в _trash (не стирается —
    можно вернуть вручную), коллекция Qdrant удаляется, из реестра — вон."""
    from datetime import datetime
    d = _dir_nocreate(name)
    trashed = ""
    if d.exists():
        trash = data_root() / "_trash"
        trash.mkdir(parents=True, exist_ok=True)
        dst = trash / f"{d.name}_{datetime.now():%Y%m%d_%H%M%S}"
        d.rename(dst)
        trashed = str(dst)
    try:
        from .index.vectorstore import VectorStore
        from .config import load_config
        store = VectorStore(load_config(), dim=1024)
        try:
            store.drop_collection(name)
        finally:
            store.close()
    except Exception:  # noqa: BLE001 — базы могло и не быть
        pass
    forget_project(name)
    return {"ok": True, "trashed": trashed}


def export_project(name: str, out_dir=None):
    """«Экспорт объекта» → zip с данными проекта (без временных исходников
    tmp_uploads; RAG-база переносится отдельно кнопками «база в/из облака»)."""
    import zipfile
    from datetime import datetime
    from pathlib import Path
    from .paths import project_dir
    d = project_dir(name)
    out_dir = Path(out_dir) if out_dir else (data_root() / "exports")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"ОБЪЕКТ_{slugify(name)}_{datetime.now():%Y%m%d_%H%M}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("_project.json", json.dumps({"project": name}, ensure_ascii=False))
        for p in sorted(d.rglob("*")):
            if p.is_dir() or any(part in _SKIP_DIRS for part in p.relative_to(d).parts):
                continue
            zf.write(p, str(p.relative_to(d)).replace("\\", "/"))
    return out


def import_project(zip_path, *, name: str | None = None) -> str:
    """«Загрузить объект» из zip экспорта. Если объект с таким именем уже есть —
    импортируется под именем «… (импорт)», ничего не затирается."""
    import zipfile
    from pathlib import Path
    with zipfile.ZipFile(str(zip_path)) as zf:
        names = zf.namelist()
        if not name:
            if "_project.json" in names:
                name = json.loads(zf.read("_project.json").decode("utf-8")).get("project")
            elif "inventory.json" in names:
                name = json.loads(zf.read("inventory.json").decode("utf-8")).get("project")
        name = (name or Path(str(zip_path)).stem).strip()
        base = name
        if _dir_nocreate(name).exists():
            name = f"{base} (импорт)"
            k = 2
            while _dir_nocreate(name).exists() and k < 1000:
                name = f"{base} (импорт {k})"
                k += 1
        d = _dir_nocreate(name)
        d.mkdir(parents=True, exist_ok=True)
        for m in names:
            if m == "_project.json" or m.endswith("/"):
                continue
            rel = Path(m.replace("\\", "/"))
            if ".." in rel.parts or rel.is_absolute():
                continue
            dest = d / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(m))
    # inventory хранит имя проекта — обновляем под новое имя
    inv = d / "inventory.json"
    if inv.exists():
        try:
            data = json.loads(inv.read_text(encoding="utf-8"))
            data["project"] = name
            inv.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    register_project(name)
    return name
