"""№10-1: извлечение контактов проектировщиков и экспертов из разделов ПД.

Читаются первые страницы файлов ПД (tmp_uploads) и файла замечаний (remarks/):
роли (ГИП, Разработал, Проверил, Эксперт…), ФИО «Фамилия И.О.», телефоны,
email, организации «ООО/АО …». Результат — contacts.json проекта, редактируется
таблицей в Модуле 1.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..paths import project_paths

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?:\+7|8)[\s(]*\d{3}[)\s-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}")
_ROLE = re.compile(
    r"(?im)^[ \t]*(?P<role>ГИП|Главный инженер проекта|Главный специалист|Разработал|"
    r"Выполнил|Проверил|Н\.?\s?контр\.?|Нормоконтроль|Директор|Гл\.?\s?эксперт|Эксперт)"
    r"\b[:\s.\-—]*(?P<name>[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.?|"
    r"\s+[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?)?)?")
_NAME_IO = re.compile(r"[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.?")
_ORG = re.compile(r"(?:ООО|АО|ПАО|ЗАО|ФАУ|ГАУ|ФГБУ)\s*[«\"][^»\"]{3,80}[»\"]")

_EXTS = (".docx", ".doc", ".pdf", ".txt", ".rtf")


def _first_pages_text(path: Path, cfg, pages: int = 3) -> str:
    from .loaders import extract_file
    try:
        pg = extract_file(path, ocr=False, min_text_chars=0,
                          lang=cfg.get("ocr.lang", "rus+eng"))
    except Exception:  # noqa: BLE001
        return ""
    return "\n".join(p.get("text", "") for p in pg[:pages])


def extract_contacts(project: str, cfg) -> dict[str, Any]:
    paths = project_paths(project)
    people: list[dict] = []
    orgs: set[str] = set()
    seen: set[tuple[str, str]] = set()

    def scan(folder: Path | None, kind: str) -> None:
        if not folder or not folder.exists():
            return
        for fp in sorted(folder.rglob("*")):
            if not fp.is_file() or fp.suffix.lower() not in _EXTS:
                continue
            txt = _first_pages_text(fp, cfg)
            if not txt:
                continue
            for m in _ORG.finditer(txt):
                orgs.add(re.sub(r"\s+", " ", m.group(0)))
            emails = _EMAIL.findall(txt)
            phones = [re.sub(r"\s+", " ", x.strip()) for x in _PHONE.findall(txt)]
            added_here = []
            for m in _ROLE.finditer(txt):
                role = m.group("role").strip()
                name = (m.group("name") or "").strip()
                if not name:
                    nm = _NAME_IO.search(txt[m.end(): m.end() + 90])
                    name = nm.group(0) if nm else ""
                key = (role.lower(), name.lower())
                if name and key not in seen:
                    seen.add(key)
                    rec = {"роль": role, "ФИО": name, "телефон": "", "email": "",
                           "тип": kind, "файл": fp.name}
                    people.append(rec)
                    added_here.append(rec)
            for rec in added_here:
                if not rec["email"] and emails:
                    rec["email"] = emails[0]
                if not rec["телефон"] and phones:
                    rec["телефон"] = phones[0]

    scan(paths["uploads"], "проектировщик")
    scan(paths.get("remarks_dir"), "эксперт")
    data = {"люди": people, "организации": sorted(orgs)}
    paths["contacts"].write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
    return data


def load_contacts(project: str) -> dict[str, Any]:
    p = project_paths(project)["contacts"]
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"люди": [], "организации": []}


def save_contacts(project: str, data: dict[str, Any]) -> None:
    project_paths(project)["contacts"].write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
