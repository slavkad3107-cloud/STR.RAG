"""МОДУЛЬ 6: выгрузка данных для УПРЗА «Эколог» / ИНТЕГРАЛ.

Назначение: подготовить данные о выбросах из откорректированного раздела ПМООС
для последующего расчёта рассеивания и формирования карт в УПРЗА «Эколог»
(серия программ фирмы «Интеграл»).

Честная оговорка по охвату: УПРЗА «Эколог» — коммерческое Windows-ПО без
открытого публичного API и со своим версионно-зависимым форматом обмена.
Поэтому модуль формирует:
  1. uprza_istochniki.csv  — таблица источников выбросов (геометрия — плейсхолдеры
     для ручного заполнения по проекту; коды/наименования ЗВ — извлечённые);
  2. uprza_vybrosy.csv     — перечень загрязняющих веществ с кодами ЗВ и
     значениями г/с и т/год (там, где удалось распознать);
  3. ЗАДАНИЕ_УПРЗА.txt      — инструкция по импорту и список полей для дозаполнения.

Данные берутся из answers.json (ответы + правки + фрагменты-источники),
который всегда формируется Модулем 4. Дополнительно, если доступен индекс,
можно расширить выборку (не требуется для базовой работы).
"""
from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ..paths import project_paths
from ..entities import find_pollutants, pollutant_code

_GS_RE = re.compile(r"(\d+[.,]?\d*)\s*г/с", re.IGNORECASE)
_TY_RE = re.compile(r"(\d+[.,]?\d*)\s*т/год", re.IGNORECASE)


def _answers(project: str) -> dict[str, Any]:
    from ..pipeline.block1_answers import load_answers
    return load_answers(project)


def _answer_corpus(data: dict) -> str:
    """Склеить весь распознаваемый текст ответов/правок/источников проекта."""
    parts: list[str] = []
    for a in data.get("answers", []):
        parts.append(a.get("answer", "") or "")
        parts.append(a.get("correction", "") or "")
        parts.append(a.get("user_answer", "") or "")
        for s in a.get("sources", []) or []:
            parts.append(s.get("snippet", "") or "")
    return "\n".join(p for p in parts if p)


def _match_code(name: str) -> tuple[str, str]:
    return pollutant_code(name)


def collect_emissions(project: str) -> tuple[list[dict], dict]:
    """Извлечь распознанные ЗВ с кодами и значениями г/с, т/год (если есть)."""
    data = _answers(project)
    text = _answer_corpus(data)

    # карта вещество → ближайшие значения г/с и т/год в тексте (грубая привязка)
    gs_vals = [m.group(0) for m in _GS_RE.finditer(text)]
    ty_vals = [m.group(0) for m in _TY_RE.finditer(text)]

    rows: list[dict] = []
    for p in find_pollutants(text):  # уже уникальные {code, name} из справочника
        rows.append({
            "code": p["code"],
            "name": p["name"],
            "g_s": "",          # дозаполняется по расчёту
            "t_year": "",       # дозаполняется по расчёту
        })
    quantities = sorted({m.group(0) for m in _GS_RE.finditer(text)} |
                        {m.group(0) for m in _TY_RE.finditer(text)})
    return rows, {"g_s_found": gs_vals, "t_year_found": ty_vals, "quantities": quantities}


_ZV_CODE_RE = re.compile(r"^\d{1,4}$")  # код ЗВ — числовой, обычно 3–4 знака


def validate_pollutants(rows: list[dict]) -> dict:
    """Валидация кодов ЗВ перед выгрузкой (оптимизация М6).

    Коды берутся из реестра ЗВ, но перед отправкой в УПРЗА проверяем непустоту,
    числовой формат кода и дубли — частые причины отклонений/ошибок ввода.
    Аннотирует каждую строку полем 'status' (in place) и возвращает сводку.
    """
    seen: dict[str, int] = {}
    problems: list[str] = []
    for r in rows:
        code = str(r.get("code") or "").strip()
        # find_pollutants отдаёт '—' для ЗВ без кода в реестре — это «нет кода», а не формат
        if not code or code == "—":
            r["status"] = "проверить: нет кода"
            problems.append(f"«{r.get('name', '?')}» — код ЗВ не определён")
        elif not _ZV_CODE_RE.match(code):
            r["status"] = "проверить: формат кода"
            problems.append(f"«{r.get('name', '?')}» — необычный код «{code}»")
        else:
            r["status"] = "ok"
        seen[code] = seen.get(code, 0) + 1
    dups = sorted(c for c, n in seen.items() if c and c != "—" and n > 1)
    for r in rows:
        if str(r.get("code") or "").strip() in dups and r.get("status") == "ok":
            r["status"] = "проверить: дубль кода"
    for c in dups:
        problems.append(f"код {c} встречается несколько раз")
    ok = sum(1 for r in rows if r.get("status") == "ok")
    return {"ok": ok, "to_check": len(rows) - ok, "problems": problems, "duplicates": dups}


# ─────────────────────────────── запись файлов ───────────────────────────────
def _write_sources_csv(path: Path) -> None:
    headers = [
        "N_источника", "Тип (1-точечный,2-линейный,3-площадной)", "Наименование",
        "Высота_м", "Диаметр_м", "X1", "Y1", "X2", "Y2",
        "Скорость_ГВС_м_с", "Объём_ГВС_м3_с", "Температура_ГВС_C",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(headers)
        # строки-плейсхолдеры (геометрия источников берётся из проекта вручную)
        for n in range(1, 4):
            w.writerow([f"600{n}", "", "", "", "", "", "", "", "", "", "", ""])


def _write_pollutants_csv(path: Path, rows: list[dict]) -> None:
    headers = ["Код_ЗВ", "Наименование_ЗВ", "Выброс_г_с", "Выброс_т_год", "N_источника", "Статус"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(headers)
        if not rows:
            w.writerow(["—", "ЗВ не распознаны — заполните вручную по таблице выбросов ПМООС", "", "", "", ""])
            return
        for r in rows:
            w.writerow([r["code"], r["name"], r["g_s"], r["t_year"], "", r.get("status", "")])


def _write_task_txt(path: Path, project: str, rows: list[dict], extra: dict,
                    validation: dict | None = None) -> None:
    lines = [
        "ЗАДАНИЕ НА ВНЕСЕНИЕ ДАННЫХ В УПРЗА «ЭКОЛОГ» / ИНТЕГРАЛ",
        "=" * 60,
        f"Проект: {project}",
        f"Сформировано: {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        f"Источник данных: раздел ПМООС (ответы Модуля 4).",
        "",
        "ФАЙЛЫ ВЫГРУЗКИ:",
        "  • uprza_istochniki.csv — источники выбросов (разделитель ';', UTF-8-BOM);",
        "  • uprza_vybrosy.csv    — загрязняющие вещества по источникам.",
        "",
        "ПОРЯДОК РАБОТЫ:",
        "  1. Создайте/откройте проект в УПРЗА «Эколог».",
        "  2. Внесите источники выбросов: тип, высоту, диаметр устья, координаты,",
        "     параметры газовоздушной смеси (скорость, объём, температуру).",
        "     В выгрузке геометрия оставлена пустой — заполняется по данным ПОС/ТКР.",
        "  3. Для каждого источника задайте перечень ЗВ и мощности выброса (г/с) и",
        "     валовые (т/год) из таблиц раздела ПМООС.",
        "  4. Выполните расчёт рассеивания, постройте карты рассеивания и СЗЗ.",
        "",
        f"РАСПОЗНАННЫЕ ЗАГРЯЗНЯЮЩИЕ ВЕЩЕСТВА ({len(rows)}):",
    ]
    if rows:
        for r in rows:
            lines.append(f"  – [{r['code']}] {r['name']}")
    else:
        lines.append("  (не распознано автоматически — заполните вручную)")

    if validation:
        lines += [
            "",
            f"ПРОВЕРКА КОДОВ ЗВ: ок — {validation.get('ok', 0)}, "
            f"требуют проверки — {validation.get('to_check', 0)}.",
        ]
        for prob in validation.get("problems", [])[:30]:
            lines.append(f"  ⚠ {prob}")

    if extra.get("g_s_found") or extra.get("t_year_found"):
        lines += [
            "",
            "ОБНАРУЖЕННЫЕ ЧИСЛОВЫЕ ЗНАЧЕНИЯ ВЫБРОСОВ (для сверки оператором):",
            "  г/с: " + (", ".join(extra["g_s_found"][:30]) or "—"),
            "  т/год: " + (", ".join(extra["t_year_found"][:30]) or "—"),
        ]
    lines += [
        "",
        "ВНИМАНИЕ: автоматическая привязка значений к конкретным источникам/веществам",
        "не выполняется — коды ЗВ и значения проверяет инженер по таблицам ПМООС.",
        "",
        "⚠ СПРАВОЧНИК ЗВ НЕ ИСЧЕРПЫВАЮЩИЙ: распознаются только вещества из встроенного",
        "  списка (data/entities/pollutants.yaml). Вещество без совпадения в списке в эту",
        "  выгрузку НЕ ПОПАДЁТ. Обязательно сверьте перечень выбросов с таблицами ПМООС",
        "  и действующим перечнем ЗВ (распоряжение Правительства РФ №2909-р); недостающие",
        "  вещества добавьте в справочник или внесите в УПРЗА вручную.",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def build_uprza_export(project: str) -> dict[str, Path]:
    """Сформировать выгрузку для УПРЗА. Возвращает словарь путей."""
    rows, extra = collect_emissions(project)
    validation = validate_pollutants(rows)  # аннотирует rows полем status (М6)
    out_dir = project_paths(project)["out"]
    out_dir.mkdir(parents=True, exist_ok=True)

    src = out_dir / "uprza_istochniki.csv"
    pol = out_dir / "uprza_vybrosy.csv"
    task = out_dir / "ЗАДАНИЕ_УПРЗА.txt"

    _write_sources_csv(src)
    _write_pollutants_csv(pol, rows)
    _write_task_txt(task, project, rows, extra, validation)

    return {"istochniki": src, "vybrosy": pol, "zadanie": task}
