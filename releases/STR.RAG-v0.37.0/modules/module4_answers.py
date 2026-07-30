"""МОДУЛЬ 4 — поиск ответов на замечания ПМООС (3 блока) + принятие решений.

Блок 1: найти ответ по каждому замечанию с указанием источника (раздел/файл/стр.).
Блок 2: проверить принятые правки — расчёты, ссылки, актуальность нормативов.
Блок 3: финальная проверка раздела ПМООС в целом (готовность к экспертизе).
Решение по каждому пункту принимает пользователь (human-in-the-loop).

Примеры:
  python modules/module4_answers.py --project "X" --remarks "замечания.docx" --object-type линейный --block 1
  python modules/module4_answers.py --project "X" --list
  python modules/module4_answers.py --project "X" --accept 5
  python modules/module4_answers.py --project "X" --edit 7 --text "Уточнённый ответ…"
  python modules/module4_answers.py --project "X" --reject 9
  python modules/module4_answers.py --project "X" --block 2
  python modules/module4_answers.py --project "X" --block 3
"""
from __future__ import annotations

import argparse

from _common import banner, kv  # type: ignore

_CONF = {"high": "высокая", "medium": "средняя", "low": "низкая"}


def _progress(done: int, total: int, msg: str) -> None:
    print(f"  [{done}/{total}] {msg}")


def _list_answers(project: str) -> None:
    from pmoos.pipeline.block1_answers import load_answers
    data = load_answers(project)
    answers = data.get("answers", [])
    if not answers:
        print("  Ответы ещё не сформированы (запустите --block 1).")
        return
    banner(f"Ответы по замечаниям: {project}  (всего {len(answers)})")
    for a in answers:
        st = a.get("status", "proposed")
        flag = {"accepted": "✓ принято", "edited": "✎ правлено",
                "rejected": "✗ отклонено", "proposed": "· предложено"}.get(st, st)
        conf = _CONF.get(a.get("confidence", ""), a.get("confidence", ""))
        cons = a.get("consistency", {})
        warn = "" if cons.get("ok", True) else "  ⚠ расхождения"
        print(f"\n  №{a.get('number','?')}  [{flag}]  увер.: {conf}{warn}")
        print(f"     Замечание: {a.get('remark','')[:100]}")
        ans = a.get("user_answer") or a.get("answer", "")
        print(f"     Ответ: {ans[:160]}")
        srcs = a.get("sources", [])
        if srcs:
            s0 = srcs[0]
            print(f"     Источник: раздел {s0.get('section','?')}; файл {s0.get('file','?')}; {s0.get('loc','')}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Модуль 4: ответы на замечания ПМООС")
    ap.add_argument("--project", required=True)
    ap.add_argument("--remarks", help="Путь к файлу замечаний (для блока 1)")
    ap.add_argument("--object-type", choices=["площадной", "линейный"], default=None)
    ap.add_argument("--block", choices=["1", "2", "3", "all"], help="Какой блок запустить")
    ap.add_argument("--list", action="store_true", help="Показать текущие ответы и статусы")
    ap.add_argument("--accept", metavar="N", help="Принять предложение по замечанию N")
    ap.add_argument("--reject", metavar="N", help="Отклонить предложение по замечанию N")
    ap.add_argument("--edit", metavar="N", help="Заменить ответ по замечанию N (вместе с --text)")
    ap.add_argument("--text", help="Текст пользовательского ответа (для --edit)")
    ap.add_argument("--kb", action="store_true", help="Показать размер памяти экспертизы")
    ap.add_argument("--learn", action="store_true",
                    help="Занести принятые/правленые ответы проекта в память экспертизы")
    args = ap.parse_args(argv)

    project = args.project

    if args.kb or args.learn:
        from pmoos.memory import kb_size, record_accepted
        if args.learn:
            n = record_accepted(project)
            kv("Добавлено в память", f"{n} принятых ответов")
        kv("Всего в памяти экспертизы", f"{kb_size()} записей (по всем проектам)")
        return 0

    # — решения пользователя —
    if args.accept or args.reject or args.edit:
        from pmoos.pipeline.block1_answers import set_decision
        if args.accept:
            set_decision(project, args.accept, status="accepted")
            kv("Принято", f"замечание №{args.accept}")
        if args.reject:
            set_decision(project, args.reject, status="rejected")
            kv("Отклонено", f"замечание №{args.reject}")
        if args.edit:
            if not args.text:
                print("Для --edit укажите --text \"...\"")
                return 2
            set_decision(project, args.edit, status="edited", user_answer=args.text)
            kv("Отредактировано", f"замечание №{args.edit}")
        return 0

    if args.list:
        _list_answers(project)
        return 0

    if not args.block:
        _list_answers(project)
        return 0

    # — запуск блоков —
    if args.block in ("1", "all"):
        from pmoos.pipeline.block1_answers import run_block1
        banner(f"Блок 1 — поиск ответов: {project}")
        out = run_block1(project, remarks_path=args.remarks,
                         object_type=args.object_type, progress=_progress)
        kv("Сформировано ответов", out.get("count", 0))
        _list_answers(project)

    if args.block in ("2", "all"):
        from pmoos.pipeline.block2_review import run_block2
        banner(f"Блок 2 — проверка правок/расчётов/нормативов: {project}")
        out = run_block2(project, progress=_progress)
        for r in out.get("reviews", []):
            verdict = r.get("verdict", "?")
            imp = r.get("improvements")
            imp_txt = ", ".join(imp[:2]) if isinstance(imp, list) else str(imp or "")
            print(f"  №{r.get('number','?')}: {verdict}" + (f" — {imp_txt}" if imp_txt else ""))

    if args.block in ("3", "all"):
        from pmoos.pipeline.block3_final import run_block3
        banner(f"Блок 3 — финальная проверка раздела: {project}")
        out = run_block3(project, object_type=args.object_type)
        kv("Готовность к экспертизе", out.get("ready", "?"))
        if out.get("summary"):
            print("\n  Резюме:", out["summary"])
        for issue in out.get("open_issues", [])[:10]:
            print(f"   • {issue}")
        miss = out.get("missing_required_sections", [])
        if miss:
            print("\n  Отсутствующие обязательные разделы:")
            for m in miss:
                print(f"   · [{m.get('code','')}] {m.get('name','')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
