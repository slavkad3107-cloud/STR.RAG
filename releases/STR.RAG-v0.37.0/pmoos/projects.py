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
