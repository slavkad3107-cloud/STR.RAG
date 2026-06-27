"""МОДУЛЬ 2 — построение RAG-базы (индексация) с прогрессом/паузой/возобновлением.

База Qdrant хранится ОТДЕЛЬНО от приложения (в каталоге данных), поэтому
повторный запуск не переиндексирует уже загруженное. Индексация переживает
закрытие вкладки (фоновый процесс) и возобновляется после перезапуска.

Примеры:
  python modules/module2_index.py --project "X" --object-type линейный        # в текущем окне (с прогрессом)
  python modules/module2_index.py --project "X" --background                  # в фоне (переживёт закрытие)
  python modules/module2_index.py --project "X" --status                      # показать прогресс
  python modules/module2_index.py --project "X" --pause                       # поставить на паузу
  python modules/module2_index.py --project "X" --resume                      # возобновить (в фоне)
"""
from __future__ import annotations

import argparse
import time

from _common import banner, kv  # type: ignore


def _print_status(project: str) -> None:
    from pmoos.index.indexer import progress_summary
    s = progress_summary(project)
    kv("Статус", s["status"] + (" (выполняется)" if s["running"] else ""))
    kv("Файлы", f"{s['files_done']} / {s['files_total']}  ({s['percent']}%)")
    kv("Чанков проиндексировано", s["chunks_done"])
    if s.get("current_file"):
        kv("Текущий файл", s["current_file"])
    if s.get("message"):
        kv("Сообщение", s["message"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Модуль 2: индексация RAG-базы проекта")
    ap.add_argument("--project", required=True)
    ap.add_argument("--object-type", choices=["площадной", "линейный"], default=None)
    ap.add_argument("--background", action="store_true", help="Запустить в фоне (переживёт закрытие окна)")
    ap.add_argument("--status", action="store_true", help="Показать прогресс и выйти")
    ap.add_argument("--pause", action="store_true", help="Запросить паузу")
    ap.add_argument("--resume", action="store_true", help="Снять паузу и продолжить (в фоне)")
    args = ap.parse_args(argv)

    from pmoos.index.indexer import (
        run_indexing, start_background, progress_summary,
        request_pause, clear_pause, is_running,
    )

    project = args.project

    if args.status:
        banner(f"Индексация — статус: {project}")
        _print_status(project)
        return 0

    if args.pause:
        request_pause(project)
        kv("Пауза", "запрошена — индексация остановится после текущего файла")
        return 0

    if args.resume:
        clear_pause(project)
        if is_running(project):
            kv("Внимание", "индексация уже выполняется")
            return 0
        pid = start_background(project, object_type=args.object_type)
        kv("Возобновлено в фоне", f"PID {pid}")
        return 0

    if args.background:
        if is_running(project):
            kv("Внимание", "индексация уже выполняется")
            return 0
        pid = start_background(project, object_type=args.object_type)
        banner(f"Индексация запущена в фоне: {project}")
        kv("PID", pid)
        print("  Прогресс:  python modules/module2_index.py --project \"%s\" --status" % project)
        return 0

    # передний план с живым прогрессом
    if is_running(project):
        kv("Внимание", "индексация уже выполняется (см. --status)")
        return 0

    banner(f"Индексация (в текущем окне): {project}")
    import threading
    result: dict = {}

    def _worker():
        try:
            result["state"] = run_indexing(project, object_type=args.object_type)
        except Exception as exc:  # noqa: BLE001
            result["error"] = str(exc)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    last = -1.0
    while t.is_alive():
        s = progress_summary(project)
        if s["percent"] != last:
            print(f"  [{s['percent']:5.1f}%] файлов {s['files_done']}/{s['files_total']} "
                  f"· чанков {s['chunks_done']} · {s.get('current_file','')[:40]}")
            last = s["percent"]
        time.sleep(1.5)
    t.join()

    if result.get("error"):
        print(f"\n  ОШИБКА индексации: {result['error']}")
        return 1
    banner("Индексация завершена")
    _print_status(project)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
