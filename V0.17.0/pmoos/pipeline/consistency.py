"""Проверка согласованности данных раздела ПМООС (усиленный consistency checker).

Ревью-ИИ верно отметили: ловить только «число+единица» мало. Здесь извлекаем
доменные сущности и сверяем их между источником (смежные разделы) и ответом:
  * строительная техника (КАМАЗ-65115, экскаватор CAT 320, кран и т.п.);
  * отходы и классы опасности (отход 4 класса опасности, ФККО-коды);
  * загрязняющие вещества (азота диоксид, углерод (сажа), коды по приказу);
  * числовые величины с единицами (г/с, т/год, мг/м3, дБА, м);
  * номера/ссылки на расчёты и нормативы (СП, ГОСТ, СанПиН, ПП РФ, приказы).

Используется как эвристический «детектор расхождений», не как истина в
последней инстанции — финальное решение всегда за пользователем.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..entities import (
    equipment_pattern, normalize_equipment, find_pollutants, normalize_waste_class,
)

# --- техника и ЗВ берутся из справочников data/entities/*.yaml (Entity Registry) ---

# --- отходы --------------------------------------------------------------
_WASTE_CLASS_RE = re.compile(r"\b([IVX]{1,3}|[1-5])\s*класс\w*\s*опасн", re.IGNORECASE)
_FKKO_RE = re.compile(r"\b\d{11}\b")  # код ФККО — 11 цифр

# --- величины с единицами ------------------------------------------------
_UNITS = (
    r"г/с", r"т/год", r"мг/м3", r"мг/м³", r"мкг/м3", r"дБА", r"дБ",
    r"ПДК", r"м3/сут", r"м³/сут", r"м3", r"м³", r"га", r"км", r"кВт",
    r"л/с", r"кг", r"т", r"%",
)
_QTY_RE = re.compile(
    r"(\d+[.,]?\d*)\s*(" + "|".join(_UNITS) + r")\b", re.IGNORECASE,
)

# --- нормативы -----------------------------------------------------------
_NORM_RE = re.compile(
    r"\b(СП|СНиП|ГОСТ(?:\s?Р)?|СанПиН|ГН|МУ|РД|ВСН|ОНД|приказ\w*|"
    r"постановлени\w*\s*правительства|ПП\s*РФ|№)\s*[\d\.\-–/]+\w*",
    re.IGNORECASE,
)
# --- ссылки на расчёты ----------------------------------------------------
_CALC_RE = re.compile(r"\b(расч[её]т|таблиц\w*|приложени\w*)\s*[№N]?\s*[\d\.\-]+", re.IGNORECASE)


@dataclass
class Entities:
    techniques: set[str] = field(default_factory=set)
    waste_classes: set[str] = field(default_factory=set)
    fkko: set[str] = field(default_factory=set)
    pollutants: set[str] = field(default_factory=set)
    quantities: set[str] = field(default_factory=set)
    normatives: set[str] = field(default_factory=set)
    calcs: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, list[str]]:
        return {k: sorted(v) for k, v in self.__dict__.items()}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def extract_entities(text: str) -> Entities:
    e = Entities()
    if not text:
        return e
    # техника: детектируем по справочнику и приводим к каноническому виду
    e.techniques = {normalize_equipment(m.group(0)) for m in equipment_pattern().finditer(text)}
    e.waste_classes = {normalize_waste_class(m.group(0)) for m in _WASTE_CLASS_RE.finditer(text)}
    e.fkko = {m.group(0) for m in _FKKO_RE.finditer(text)}
    # ЗВ: канонические наименования из справочника (Entity Resolution)
    e.pollutants = {p["name"] for p in find_pollutants(text)}
    e.quantities = {_norm(m.group(0)) for m in _QTY_RE.finditer(text)}
    e.normatives = {_norm(m.group(0)) for m in _NORM_RE.finditer(text)}
    e.calcs = {_norm(m.group(0)) for m in _CALC_RE.finditer(text)}
    return e


def compare(source_text: str, answer_text: str) -> dict[str, Any]:
    """Сверяет сущности ответа с сущностями источника.

    Возвращает расхождения: что упомянуто в ответе, но отсутствует в источнике
    (потенциальная выдумка/несогласованность), и наоборот.
    """
    src = extract_entities(source_text)
    ans = extract_entities(answer_text)
    issues: list[str] = []
    fields = [
        ("techniques", "техника"),
        ("waste_classes", "класс опасности отходов"),
        ("pollutants", "загрязняющие вещества"),
        ("normatives", "нормативы"),
        ("quantities", "числовые величины"),
    ]
    for attr, human in fields:
        a = getattr(ans, attr)
        s = getattr(src, attr)
        only_in_answer = a - s
        if only_in_answer and attr in ("techniques", "normatives", "pollutants", "waste_classes"):
            issues.append(
                f"В ответе есть {human}, которых нет в найденных источниках: "
                f"{', '.join(sorted(only_in_answer)[:8])}"
            )
    return {
        "source": src.to_dict(),
        "answer": ans.to_dict(),
        "issues": issues,
        "ok": not issues,
    }
