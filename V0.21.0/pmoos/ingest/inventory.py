"""МОДУЛЬ 1 (ядро): систематизация проектной документации по ПП-87.

Что делает:
  • сканирует загруженные файлы (во временной папке проекта);
  • по имени файла определяет раздел ПД (ТКР/ПОС/ИЭИ/ПМООС/…) и подсказку версии;
  • строит inventory.json — карту РАЗДЕЛОВ проекта (а не просто список файлов):
    какие разделы присутствуют, какие отсутствуют (по требованиям ПП-87 для
    площадного/линейного объекта), сколько файлов в каждом разделе;
  • хранит ТОЛЬКО метаданные (имя, размер, дата, предполагаемый раздел и версия) —
    содержимое файлов не сохраняется (требование пользователя #9);
  • контакты проектировщиков/экспертов (contacts.json).

Требования пользователя, учтённые здесь:
  #5/#8 — систематизация по РАЗДЕЛУ; авто-догадка раздела даётся как кандидаты,
          но пользователь может переопределить (set_file_section) — не навязываем;
  #6    — состав разделов берётся из полного перечня ПП-87 (ingest.sections);
  #9    — сохраняем только имена и метаданные, не сами файлы.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..paths import project_paths
from .sections import (
    classify_filename, detect_version_hint, required_sections,
    section_name, guess_object_type,
)
from .loaders import SUPPORTED_EXT


def _iter_files(upload_dir: Path) -> list[Path]:
    if not upload_dir.exists():
        return []
    out = []
    for fp in sorted(upload_dir.rglob("*")):
        if fp.is_file() and fp.suffix.lower() in SUPPORTED_EXT:
            out.append(fp)
    return out


def build_inventory(project: str, *, uploads_dir: str | Path | None = None,
                    object_type: str | None = None) -> dict[str, Any]:
    paths = project_paths(project)
    up = Path(uploads_dir) if uploads_dir else paths["uploads"]
    files = _iter_files(up)
    names = [f.name for f in files]

    if not object_type:
        object_type = guess_object_type(names) if names else "площадной"

    # ранее заданные пользователем переопределения раздела/версии сохраняем
    prev = load_inventory(project) or {}
    overrides = prev.get("overrides", {})
    version_overrides = prev.get("version_overrides", {})

    file_items: list[dict] = []
    for f in files:
        rel = str(f.relative_to(up)) if up in f.parents or f.parent == up else f.name
        cands = classify_filename(f.name, object_type, top=3)
        best = overrides.get(rel) or (cands[0]["code"] if cands else "UNKNOWN")
        try:
            st = f.stat()
            size, mtime = st.st_size, datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
        except OSError:
            size, mtime = 0, ""
        vhint = detect_version_hint(f.name)
        item = {
            "name": f.name,
            "rel": rel,
            "section": best,
            "section_name": section_name(best) if best != "UNKNOWN" else "—",
            "candidates": cands,
            "object_type": object_type,          # тип объекта (для отображения в М1)
            "version_hint": vhint,               # авто-подсказка версии
            "size": size,
            "mtime": mtime,
        }
        if rel in version_overrides:             # версия, заданная пользователем
            item["version_override"] = version_overrides[rel]
        file_items.append(item)

    present = sorted({it["section"] for it in file_items if it["section"] and it["section"] != "UNKNOWN"})
    req = required_sections(object_type)
    req_codes = [r["code"] for r in req]
    missing = [c for c in req_codes if c not in present]

    inv = {
        "project": project,
        "object_type": object_type,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "files": file_items,
        "overrides": overrides,
        "version_overrides": version_overrides,
        "sections_present": present,
        "sections_required": req_codes,
        "sections_missing": missing,
    }
    paths["inventory"].parent.mkdir(parents=True, exist_ok=True)
    paths["inventory"].write_text(json.dumps(inv, ensure_ascii=False, indent=2), encoding="utf-8")
    return inv


def load_inventory(project: str) -> dict[str, Any] | None:
    p = project_paths(project)["inventory"]
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def set_file_section(project: str, file_rel: str, section_code: str) -> dict[str, Any]:
    """Переопределить раздел для файла (пользователь подтверждает/исправляет догадку)."""
    inv = load_inventory(project) or {"overrides": {}}
    inv.setdefault("overrides", {})[file_rel] = section_code
    for it in inv.get("files", []):
        if it["rel"] == file_rel:
            it["section"] = section_code
            it["section_name"] = section_name(section_code) if section_code != "UNKNOWN" else "—"
    # пересчёт присутствующих/отсутствующих
    present = sorted({it["section"] for it in inv.get("files", []) if it["section"] not in ("", "UNKNOWN")})
    inv["sections_present"] = present
    inv["sections_missing"] = [c for c in inv.get("sections_required", []) if c not in present]
    project_paths(project)["inventory"].write_text(
        json.dumps(inv, ensure_ascii=False, indent=2), encoding="utf-8")
    return inv


def set_file_version(project: str, file_rel: str, version_label: str) -> dict[str, Any]:
    """Задать/исправить версию конкретного файла (пользователь меняет вручную).

    Реализует требование М1: после анализа показывать версию раздела и дать
    возможность пользователю её изменить. Хранится как version_override в файле
    инвентаря (исходная авто-подсказка version_hint сохраняется отдельно)."""
    inv = load_inventory(project)
    if not inv:
        return {}
    inv.setdefault("version_overrides", {})[file_rel] = version_label
    for it in inv.get("files", []):
        if it["rel"] == file_rel:
            it["version_override"] = version_label
    project_paths(project)["inventory"].write_text(
        json.dumps(inv, ensure_ascii=False, indent=2), encoding="utf-8")
    return inv


def section_overview(project: str, object_type: str | None = None) -> list[dict]:
    """Карта РАЗДЕЛОВ для таблицы/визуализации (а не список файлов).

    Для каждого требуемого ПП-87 раздела: есть/нет, сколько файлов, имена файлов,
    текущая версия (если есть данные версий).
    """
    inv = load_inventory(project)
    object_type = object_type or (inv.get("object_type") if inv else "площадной")
    files = inv.get("files", []) if inv else []

    by_sec: dict[str, list[dict]] = {}
    for it in files:
        by_sec.setdefault(it["section"], []).append(it)

    rows = []
    for r in required_sections(object_type):
        code = r["code"]
        secfiles = by_sec.get(code, [])
        rows.append({
            "code": code,
            "num": r.get("num", ""),
            "name": r["name"],
            "present": bool(secfiles),
            "n_files": len(secfiles),
            "files": [f["name"] for f in secfiles],
        })
    # разделы вне обязательного перечня (доп. материалы)
    extra_codes = [c for c in by_sec if c not in {r["code"] for r in required_sections(object_type)}]
    for code in sorted(extra_codes):
        if code == "UNKNOWN":
            continue
        secfiles = by_sec.get(code, [])
        rows.append({
            "code": code,
            "name": section_name(code),
            "present": True,
            "n_files": len(secfiles),
            "files": [f["name"] for f in secfiles],
            "extra": True,
        })
    return rows


# ─────────────────────────────── контакты ───────────────────────────────
def load_contacts(project: str) -> dict[str, Any]:
    p = project_paths(project)["contacts"]
    if not p.exists():
        return {"designers": [], "experts": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"designers": [], "experts": []}


def save_contacts(project: str, contacts: dict[str, Any]) -> Path:
    p = project_paths(project)["contacts"]
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "designers": contacts.get("designers", []),
        "experts": contacts.get("experts", []),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p
