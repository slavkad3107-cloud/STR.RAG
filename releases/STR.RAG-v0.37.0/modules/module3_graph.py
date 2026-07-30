"""МОДУЛЬ 3 — графические связи между разделами ПД и каскадный анализ изменений.

Строит граф зависимостей данных (ПЗУ/ТКР→ПОС→ПМООС; ПОС→Выбросы→Рассеивание→СЗЗ
и т.д.), сохраняет его и показывает, какие разделы нужно пересчитать при изменении.

Примеры:
  python modules/module3_graph.py --project "X"                       # построить и показать граф
  python modules/module3_graph.py --project "X" --changed POS,TKR     # каскад: что пересчитать
  python modules/module3_graph.py --project "X" --html                 # интерактивный граф (pyvis) в out/
"""
from __future__ import annotations

import argparse

from _common import banner, kv  # type: ignore


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Модуль 3: граф связей разделов и каскад изменений")
    ap.add_argument("--project", required=True)
    ap.add_argument("--changed", help="Коды изменённых разделов через запятую (напр. POS,TKR)")
    ap.add_argument("--html", action="store_true", help="Сохранить интерактивный граф (pyvis)")
    ap.add_argument("--knowledge", action="store_true",
                    help="Обновить накопительный граф знаний по проектам и показать статистику")
    ap.add_argument("--who-uses", metavar="ТЕКСТ",
                    help="В каких проектах встречалась техника/ЗВ (по фрагменту названия)")
    args = ap.parse_args(argv)

    from pmoos.graph.dependency import build_and_save, to_vis, write_vis_html
    from pmoos.graph.cascade import downstream, explain_cascade
    from pmoos.ingest.sections import section_name

    project = args.project

    if args.who_uses:
        from pmoos.graph.knowledge import projects_with_entity
        projs = projects_with_entity(args.who_uses)
        banner(f"Где встречалось: «{args.who_uses}»")
        for p in projs:
            print("  •", p)
        if not projs:
            print("  (не найдено в накопленном графе знаний)")
        return 0

    if args.knowledge:
        from pmoos.graph.knowledge import update_from_project, stats
        banner(f"Граф знаний по проектам: обновление из «{project}»")
        kn = update_from_project(project)
        kv("Добавлено сущностей", kn["entities"])
        s = stats()
        kv("Всего узлов", s["nodes"]); kv("Всего связей", s["edges"]); kv("По типам", s["by_kind"])
        return 0

    banner(f"Граф связей разделов: {project}")
    g = build_and_save(project)
    kv("Узлов", g.number_of_nodes())
    kv("Связей", g.number_of_edges())

    vis = to_vis(g)
    print("\n  Связи данных между разделами:")
    for e in vis["edges"]:
        print(f"   {e['from']:10s} → {e['to']:12s}  {e.get('title','')}")

    if args.changed:
        codes = [c.strip() for c in args.changed.split(",") if c.strip()]
        banner(f"Каскад изменений: {', '.join(codes)}")
        res = downstream(project, codes)
        if not res["changed"]:
            print("  Указанные коды не найдены в графе. Допустимые узлы:",
                  ", ".join(sorted(n["id"] for n in vis["nodes"])))
        else:
            print("  Затронутые разделы/расчёты (нужно перепроверить):")
            for a in res["affected"]:
                print(f"   • {a['label']:24s} глубина {a['depth']}  путь: {' → '.join(a['via'])}")
            if res.get("order"):
                kv("\n  Порядок пересчёта", " → ".join(res["order"]))

    if args.html:
        path = write_vis_html(project, vis)
        kv("Интерактивный граф", path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
