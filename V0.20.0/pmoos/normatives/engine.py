"""Движок контроля актуальности нормативных ссылок (требование ревью-ИИ).

Берёт реестр из data/normatives.yaml (+ опционально пользовательский YAML в
каталоге данных), находит в тексте ссылки на СП/ГОСТ/СанПиН/ГН/ОНД/приказы/ПП
и помечает отменённые/заменённые. Дополнительно пользователь может расширять
реестр под свою практику.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..paths import data_root

_APP_YAML = Path(__file__).resolve().parents[2] / "data" / "normatives.yaml"

# Регэксп для извлечения нормативных обозначений из текста.
# Между обозначением и номером допускаем пробел/дефис/тире (ОНД-86, СП 47…).
_REF_RE = re.compile(
    r"(?P<doc>СП|СНиП|ГОСТ(?:\s?Р)?|СанПиН|ГН|МУ|МР|РД|ВСН|ОНД|"
    r"приказ\w*\s+минприроды\w*\s+росси\w*|постановлени\w*\s+правительства\w*\s+рф|ПП\s*РФ)"
    r"[\s\-–]*(?P<num>№?\s?\d[\d\.\-/А-Яа-я]*)",
    re.IGNORECASE,
)


def _user_yaml() -> Path:
    return data_root() / "normatives_user.yaml"


@lru_cache(maxsize=1)
def _registry() -> dict[str, dict]:
    import yaml
    reg: dict[str, dict] = {}
    for path in (_APP_YAML, _user_yaml()):
        if path and path.exists():
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            for item in data.get("normatives", []):
                key = _key(item.get("id", ""))
                if key:
                    reg[key] = item
    return reg


def _key(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").lower()).replace("№", "")


def find_references(text: str) -> list[str]:
    """Возвращает уникальные обозначения нормативов, найденные в тексте."""
    refs = []
    seen = set()
    for m in _REF_RE.finditer(text or ""):
        ref = re.sub(r"\s+", " ", m.group(0)).strip().rstrip(".,;:)")
        k = _key(ref)
        if k not in seen:
            seen.add(k)
            refs.append(ref)
    return refs


def check_text(text: str) -> dict[str, Any]:
    """Сверяет ссылки в тексте с реестром.

    Возвращает:
      found: все найденные ссылки,
      problems: список проблем (отменён/заменён) с рекомендацией,
      unknown: ссылки, которых нет в реестре (нужно проверить вручную).
    """
    reg = _registry()
    found = find_references(text)
    problems, unknown = [], []
    for ref in found:
        k = _key(ref)
        # пытаемся найти точное или частичное совпадение по началу номера
        item = reg.get(k)
        if item is None:
            for rk, rv in reg.items():
                if k.startswith(rk[: max(6, len(rk) - 4)]) or rk.startswith(k[: max(6, len(k) - 4)]):
                    item = rv
                    break
        if item is None:
            unknown.append(ref)
            continue
        status = (item.get("status") or "actual").lower()
        if status in ("cancelled", "replaced"):
            problems.append({
                "ref": ref,
                "status": status,
                "replaced_by": item.get("replaced_by", ""),
                "title": item.get("title", ""),
                "note": item.get("note", ""),
                "recommendation": (
                    f"Документ '{item.get('id', ref)}' {('ОТМЕНЁН' if status=='cancelled' else 'ЗАМЕНЁН')}. "
                    + (f"Использовать: {item.get('replaced_by')}." if item.get("replaced_by") else "Уточнить действующую редакцию.")
                ),
            })
    return {"found": found, "problems": problems, "unknown": unknown,
            "ok": not problems}


def registry_size() -> int:
    return len(_registry())
