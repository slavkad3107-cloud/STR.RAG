"""Реестр доменных сущностей (Entity Registry) — ответ на замечания ревью.

Справочники техники / ЗВ / отходов вынесены в YAML (data/entities/*.yaml),
чтобы эксперт мог их пополнять без правки кода. Здесь — загрузка справочников и
нормализация (резолвинг) сущностей: «КАМАЗ-65115», «КАМАЗ 65115», «самосвал
КАМАЗ» → один канонический объект «Автосамосвал»; названия ЗВ → код + канон.

Пользовательские дополнения подхватываются из data_root()/entities/*.yaml
(перекрывают/расширяют встроенные). Если YAML нет — работают встроенные defaults.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from ..paths import data_root, APP_ROOT

_PKG_DATA = APP_ROOT / "data" / "entities"
_USER_DATA = data_root() / "entities"


def _load_yaml(name: str) -> dict[str, Any]:
    """Слить пакетный YAML с пользовательским (пользовательский — поверх)."""
    out: dict[str, Any] = {}
    for base in (_PKG_DATA, _USER_DATA):
        p = base / name
        if p.exists():
            try:
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, list) and isinstance(out.get(k), list):
                            out[k] = out[k] + v
                        else:
                            out[k] = v
            except Exception:  # noqa: BLE001
                pass
    return out


# ─────────────────────────── ЗАГРЯЗНЯЮЩИЕ ВЕЩЕСТВА ───────────────────────────
@lru_cache(maxsize=1)
def pollutants() -> list[dict]:
    data = _load_yaml("pollutants.yaml").get("pollutants")
    if data:
        return data
    return [  # минимальный fallback
        {"code": "0301", "name": "Азота диоксид (Азот (IV) оксид)", "aliases": ["азота диоксид", "диоксид азота"]},
        {"code": "0337", "name": "Углерода оксид", "aliases": ["углерода оксид", "оксид углерода"]},
        {"code": "0328", "name": "Углерод (Сажа)", "aliases": ["сажа"]},
    ]


@lru_cache(maxsize=1)
def _pollutant_index() -> list[tuple["re.Pattern[str]", str, str]]:
    """(compiled_pattern, code, canonical). Якоря не-словных границ, чтобы
    короткие формулы (no, co, no2) не ловились как подстроки в других словах."""
    idx: list[tuple[str, str, str]] = []
    for p in pollutants():
        for a in p.get("aliases", []):
            idx.append((a.lower(), p.get("code", ""), p.get("name", a)))
    idx.sort(key=lambda t: len(t[0]), reverse=True)
    out = []
    for alias, code, canon in idx:
        pat = re.compile(r"(?<![\w])" + re.escape(alias) + r"(?![\w])", re.IGNORECASE)
        out.append((pat, code, canon))
    return out


def pollutant_code(name: str) -> tuple[str, str]:
    """Имя/фрагмент → (код ЗВ, каноническое имя). ('', name) если не найдено."""
    low = (name or "")
    for pat, code, canon in _pollutant_index():
        if pat.search(low):
            return code, canon
    return "", name


def find_pollutants(text: str) -> list[dict]:
    """Найти ЗВ в тексте → список {code, name} (уникальные).

    _pollutant_index отсортирован по длине алиаса УБЫВАЮЩЕ. Отслеживаем занятые
    длинными совпадениями участки текста и пропускаем более короткие алиасы,
    целиком вложенные в уже принятый участок («углеводороды» внутри «углеводороды
    предельные»), — чтобы короткий алиас не добавлял неверный код поверх точного.
    """
    low = (text or "")
    seen: set[str] = set()
    covered: list[tuple[int, int]] = []
    out: list[dict] = []
    for pat, code, canon in _pollutant_index():
        accepted_here = False
        for m in pat.finditer(low):
            s, e = m.span()
            if any(s >= cs and e <= ce for cs, ce in covered):
                continue  # совпадение целиком внутри более длинного — пропускаем
            covered.append((s, e))
            accepted_here = True
        if accepted_here:
            key = code or canon
            if key not in seen:
                seen.add(key)
                out.append({"code": code or "—", "name": canon})
    return out


# ─────────────────────────── ТЕХНИКА ───────────────────────────
@lru_cache(maxsize=1)
def equipment() -> list[dict]:
    data = _load_yaml("equipment.yaml").get("equipment")
    if data:
        return data
    return [
        {"name": "Автосамосвал", "type": "самосвал", "aliases": ["самосвал", "камаз", "маз"]},
        {"name": "Экскаватор", "type": "экскаватор", "aliases": ["экскаватор", "cat", "komatsu"]},
        {"name": "Автокран", "type": "кран", "aliases": ["автокран", "кран"]},
    ]


@lru_cache(maxsize=1)
def _equipment_index() -> list[tuple["re.Pattern[str]", str, str]]:
    idx: list[tuple[str, str, str]] = []
    for e in equipment():
        for a in e.get("aliases", []):
            idx.append((a.lower(), e.get("name", a), e.get("type", "")))
    idx.sort(key=lambda t: len(t[0]), reverse=True)
    out = []
    for alias, canon, etype in idx:
        pat = re.compile(r"(?<![\w])" + re.escape(alias) + r"(?![\w])", re.IGNORECASE)
        out.append((pat, canon, etype))
    return out


def normalize_equipment(mention: str) -> str:
    """«КАМАЗ-65115» / «самосвал КАМАЗ» → «Автосамосвал». Иначе — исходное."""
    low = (mention or "")
    for pat, canon, _type in _equipment_index():
        if pat.search(low):
            return canon
    return mention


def find_equipment(text: str) -> list[str]:
    """Найти технику в тексте → список канонических наименований (уникальные)."""
    low = (text or "")
    seen: set[str] = set()
    out: list[str] = []
    for pat, canon, _type in _equipment_index():
        if pat.search(low) and canon not in seen:
            seen.add(canon)
            out.append(canon)
    return out


@lru_cache(maxsize=1)
def _equipment_alias_pattern() -> "re.Pattern[str]":
    aliases = sorted({a.lower() for e in equipment() for a in e.get("aliases", [])},
                     key=len, reverse=True)
    if not aliases:
        return re.compile(r"(?!x)x")
    body = "|".join(re.escape(a) for a in aliases)
    # бренд/класс + необязательная марка (цифры)
    return re.compile(r"(?<![\w])(" + body + r")[\w\-]*(?:\s*[-–]?\s*\d{2,4}[\w\-]*)?", re.IGNORECASE)


def equipment_pattern() -> "re.Pattern[str]":
    return _equipment_alias_pattern()


# ─────────────────────────── ОТХОДЫ ───────────────────────────
@lru_cache(maxsize=1)
def waste_cfg() -> dict:
    data = _load_yaml("waste.yaml")
    return data or {"class_map": {"1": "I", "2": "II", "3": "III", "4": "IV", "5": "V"}}


def normalize_waste_class(raw: str) -> str:
    """'4 класс опасности' / 'IV класса опасности' → 'IV класс опасности'."""
    cmap = waste_cfg().get("class_map", {})
    m = re.search(r"([IVX]{1,3}|[1-5])", raw or "", re.IGNORECASE)
    if not m:
        return raw
    val = m.group(1).upper()
    val = cmap.get(val, val)
    return f"{val} класс опасности"


def registry_sizes() -> dict[str, int]:
    return {"pollutants": len(pollutants()), "equipment": len(equipment())}
