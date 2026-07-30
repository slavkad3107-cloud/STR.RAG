"""МОДУЛЬ 5 — корректировка ПМООС (DOCX) и таблица ответов на замечания.

Формирует:
  • откорректированный раздел ПМООС (.docx) с правками по принятым ответам;
  • таблицу ответов на ВСЕ замечания со столбцами «ОТВЕТ» и «ИСТОЧНИК»
    (раздел/файл/страница) — в .docx и .xlsx.

Примеры:
  python modules/module5_export.py --project "X"
  python modules/module5_export.py --project "X" --oos "Раздел ПМООС.docx"
  python modules/module5_export.py --project "X" --what table
"""
from __future__ import annotations

import argparse

from _common import banner, kv  # type: ignore


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Модуль 5: корректировка ПМООС и таблица ответов")
    ap.add_argument("--project", required=True)
    ap.add_argument("--oos", help="Путь к исходному файлу раздела ПМООС (для приложения-сверки)")
    ap.add_argument("--what", choices=["corrected", "table", "all"], default="all")
    ap.add_argument("--triples", action="store_true",
                    help="Также выгрузить обучающие тройки (anchor/positive/negative) в out/")
    args = ap.parse_args(argv)

    project = args.project
    banner(f"Формирование результатов ПМООС: {project}")
    made: list[str] = []

    if args.triples:
        from pmoos.output.training_export import export_triples
        r = export_triples(project)
        kv("Обучающие тройки", f"{r['count']} → {r['path']}")

    if args.what in ("corrected", "all"):
        from pmoos.output.docx_writer import build_corrected_oos_docx
        p = build_corrected_oos_docx(project, original_oos_path=args.oos)
        kv("Откорректированный ПМООС", p)
        made.append(str(p))

    if args.what in ("table", "all"):
        from pmoos.output.answers_table import build_answers_table_docx, build_answers_table_xlsx
        d = build_answers_table_docx(project)
        x = build_answers_table_xlsx(project)
        kv("Таблица ответов (DOCX)", d)
        kv("Таблица ответов (XLSX)", x)
        made += [str(d), str(x)]

    print("\nГотово. Файлы сохранены в папке out проекта.")
    return 0 if made else 1


if __name__ == "__main__":
    raise SystemExit(main())
