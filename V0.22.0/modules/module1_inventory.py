"""МОДУЛЬ 1 — загрузка, анализ и систематизация ПД по ПП-87, версификация.

Примеры:
  python modules/module1_inventory.py --project "ОПОЧКА-ДУБРОВКА 83-26С" --uploads ./files --object-type линейный
  python modules/module1_inventory.py --project "ОПОЧКА-ДУБРОВКА 83-26С" --show
  python modules/module1_inventory.py --project "X" --set-section "ИЭИ том2.pdf=IEI"
"""
from __future__ import annotations

import argparse
import sys

from _common import banner, kv, section_table  # type: ignore


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Модуль 1: систематизация ПД (ПП-87) и версии")
    ap.add_argument("--project", required=True, help="Имя проекта")
    ap.add_argument("--uploads", help="Папка с файлами ПД (если не задано — tmp_uploads проекта)")
    ap.add_argument("--object-type", choices=["площадной", "линейный"], help="Тип объекта (влияет на состав ПД)")
    ap.add_argument("--show", action="store_true", help="Только показать текущую карту (без пересборки)")
    ap.add_argument("--set-section", help='Переопределить раздел файла: "имя_файла=КОД"')
    ap.add_argument("--versions", action="store_true", help="Показать анализ версий разделов")
    args = ap.parse_args(argv)

    from pmoos.ingest.inventory import (
        build_inventory, load_inventory, section_overview, set_file_section,
        load_contacts,
    )
    from pmoos.ingest.sections import required_sections, section_name
    from pmoos.versioning.versions import analyze_versions, change_timeline

    project = args.project

    if args.set_section:
        try:
            fname, code = args.set_section.rsplit("=", 1)
        except ValueError:
            print("Формат: --set-section \"имя_файла=КОД\"", file=sys.stderr)
            return 2
        set_file_section(project, fname.strip(), code.strip())
        kv("Раздел переопределён", f"{fname.strip()} → {code.strip()}")

    if args.show:
        inv = load_inventory(project)
        if not inv:
            print("Инвентаризация не найдена. Запустите без --show, указав --uploads.")
            return 1
    else:
        banner(f"Систематизация ПД: {project}")
        inv = build_inventory(project, uploads_dir=args.uploads, object_type=args.object_type)

    ot = inv["object_type"]
    kv("Тип объекта", ot)
    kv("Файлов учтено", len(inv.get("files", [])))
    kv("Разделов присутствует", len(inv.get("sections_present", [])))
    miss = inv.get("sections_missing", [])
    kv("Отсутствует разделов", len(miss))

    banner("Карта разделов проектной документации (ПП-87)")
    section_table(section_overview(project, ot))

    if miss:
        banner("ОТСУТСТВУЮТ обязательные разделы")
        for c in miss:
            print(f"   · [{c}] {section_name(c)}")

    # версии
    vers = analyze_versions(project, object_type=ot)
    groups = list(vers.get("groups", {}).values())
    multi = [g for g in groups if len(g.get("versions", [])) > 1]
    if args.versions or multi:
        banner("Версии разделов (актуальная отмечена ★)")
        for g in (multi or groups):
            cur = g.get("current_file")
            print(f"   {section_name(g.get('section',''))} — «{g.get('base','')}»:")
            for v in g.get("versions", []):
                star = "★" if (v.get("is_current") or v.get("file") == cur) else " "
                date = f" [{v['date']}]" if v.get("date") else ""
                print(f"      {star} {v.get('label','?'):14s} {v.get('file','')}{date}")

    tl = change_timeline(project)
    if tl:
        banner("Хронология изменений (по датам в именах/файлах)")
        for e in tl[:20]:
            print(f"   {e.get('date','—')}  {section_name(e.get('section',''))}: {e.get('file','')}")

    contacts = load_contacts(project)
    if contacts.get("designers") or contacts.get("experts"):
        banner("Контакты")
        for d in contacts.get("designers", []):
            kv("Проектировщик", f"{d.get('name','')} — {d.get('role','')} {d.get('email','')}")
        for e in contacts.get("experts", []):
            kv("Эксперт", f"{e.get('name','')} — {e.get('org','')}")

    print("\nГотово. Файлы проекта не сохраняются — хранится только карта разделов.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
