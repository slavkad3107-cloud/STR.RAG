"""МОДУЛЬ 6 — выгрузка данных о выбросах для УПРЗА «Эколог» / ИНТЕГРАЛ.

Формирует таблицы источников и загрязняющих веществ + задание на ввод данных
для расчёта рассеивания и построения карт.

Пример:
  python modules/module6_uprza.py --project "X"
"""
from __future__ import annotations

import argparse

from _common import banner, kv  # type: ignore


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Модуль 6: выгрузка для УПРЗА Эколог/ИНТЕГРАЛ")
    ap.add_argument("--project", required=True)
    args = ap.parse_args(argv)

    from pmoos.output.uprza_export import build_uprza_export, collect_emissions

    project = args.project
    banner(f"Выгрузка для УПРЗА: {project}")

    rows, extra = collect_emissions(project)
    kv("Распознано ЗВ", len(rows))
    for r in rows:
        print(f"   [{r['code']}] {r['name']}")
    if extra.get("g_s_found"):
        kv("Найдены значения г/с", ", ".join(extra["g_s_found"][:15]))
    if extra.get("t_year_found"):
        kv("Найдены значения т/год", ", ".join(extra["t_year_found"][:15]))

    paths = build_uprza_export(project)
    print()
    kv("Источники (CSV)", paths["istochniki"])
    kv("Вещества (CSV)", paths["vybrosy"])
    kv("Задание (TXT)", paths["zadanie"])
    print("\nГеометрию источников и привязку значений заполняет инженер по данным проекта.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
