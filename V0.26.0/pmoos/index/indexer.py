"""Фоновая индексация проекта в векторную БД (МОДУЛЬ 2).

Ключевые требования пользователя, которые здесь закрыты:
  * прогресс виден (сколько проиндексировано / сколько осталось) — index_state.json;
  * пауза и возобновление, переживающие закрытие вкладки и перезапуск процесса;
  * работа в фоне (отдельным процессом, не блокирующим UI);
  * БД хранится ОТДЕЛЬНО от приложения (см. paths.qdrant_dir), не индексируется заново;
  * дедупликация по sha256 содержимого (existing_doc_shas);
  * оптимизация под 3070ti — эмбеддинги на GPU, батчи, кэш.

Состояние (index_state.json):
{
  "status": "running|paused|done|error|idle",
  "pause_requested": false,
  "pid": 12345,
  "total_files": N, "done_files": M,
  "total_chunks": X, "done_chunks": Y,
  "files": {"<rel>": {"status": "pending|done|skipped|error", "chunks": n, "section": "..."}},
  "current_file": "...", "message": "...", "updated_at": "ISO"
}
"""
from __future__ import annotations

import json
import os
import sys
import time
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import threading
import traceback

from ..config import Config, load_config
from ..paths import project_paths, APP_ROOT

# Тяжёлые/доменные импорты (ingest, эмбеддинги) выполняются ЛЕНИВО внутри функций,
# чтобы фоновый процесс стартовал мгновенно и «пульс» появлялся сразу.


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def read_state(project: str) -> dict[str, Any]:
    p = project_paths(project)["index_state"]
    if not p.exists():
        return {"status": "idle", "pause_requested": False, "total_files": 0,
                "done_files": 0, "total_chunks": 0, "done_chunks": 0, "files": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"status": "idle", "pause_requested": False, "files": {}}


_WRITE_LOCK = threading.Lock()


def _write_state_unlocked(project: str, state: dict[str, Any]) -> None:
    state["updated_at"] = _now()
    state["heartbeat"] = _now()  # любая запись состояния = признак жизни процесса
    p = project_paths(project)["index_state"]
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def write_state(project: str, state: dict[str, Any]) -> None:
    with _WRITE_LOCK:
        _write_state_unlocked(project, state)


def log_path(project: str) -> Path:
    return project_paths(project)["root"] / "index_log.txt"


def log_tail(project: str, n: int = 40) -> str:
    """Последние строки журнала фоновой индексации (stdout/stderr процесса)."""
    p = log_path(project)
    if not p.exists():
        return ""
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:  # noqa: BLE001
        return ""


def _hf_model_cached(model_name: str) -> bool:
    """Есть ли модель в локальном кэше HuggingFace (без обращения к сети)."""
    try:
        base = os.environ.get("HF_HOME") or os.environ.get("HUGGINGFACE_HUB_CACHE")
        hub = Path(base) if base else Path.home() / ".cache" / "huggingface"
        if hub.name != "hub":
            hub = hub / "hub"
        return (hub / ("models--" + model_name.replace("/", "--"))).exists()
    except Exception:  # noqa: BLE001
        return False


def reset_state(project: str) -> None:
    """Сбросить «зависший» статус. Прогресс по уже обработанным файлам сохраняется."""
    st = read_state(project)
    st["status"] = "idle"
    st["pause_requested"] = True   # если процесс всё же жив — остановится на следующем файле
    st["pid"] = 0
    st["current_file"] = ""
    st["message"] = "Статус сброшен вручную. Нажмите «Индексировать», чтобы продолжить."
    write_state(project, st)


def stop_indexing(project: str) -> bool:
    """Жёстко остановить фоновую индексацию (кнопка «⏹ Стоп»).
    Завершает процесс по pid; прогресс по уже готовым файлам сохраняется."""
    st = read_state(project)
    pid = int(st.get("pid") or 0)
    killed = False
    if pid and _pid_alive(pid):
        try:
            if os.name == "nt":
                import ctypes
                PROCESS_TERMINATE = 0x0001
                h = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
                if h:
                    killed = bool(ctypes.windll.kernel32.TerminateProcess(h, 1))
                    ctypes.windll.kernel32.CloseHandle(h)
            else:
                import signal
                os.kill(pid, signal.SIGTERM)
                killed = True
        except Exception:  # noqa: BLE001
            pass
    # файл, обрабатывавшийся в момент жёсткого стопа, мог остаться с частично
    # записанными чанками → помечаем ошибкой, чтобы при возобновлении его чанки
    # подчистились (delete_by_file) и он переиндексировался целиком.
    cur = st.get("current_file") or ""
    if cur:
        st.setdefault("files", {})[cur] = {
            "status": "error", "chunks": 0,
            "error": "прервано кнопкой «Стоп» — будет переиндексирован при возобновлении",
        }
    st["status"] = "paused"
    st["pause_requested"] = True
    st["pid"] = 0
    st["current_file"] = ""
    st["message"] = "⏹ Остановлено пользователем. «⏯ Продолжить» возобновит с места остановки."
    write_state(project, st)
    return killed


def _start_heartbeat(project: str) -> None:
    """Фоновый «пульс»: каждые 5 с обновляет heartbeat в состоянии. Бьётся даже во
    время долгой загрузки модели — по нему видно, что процесс жив, а не завис."""
    def beat() -> None:
        while True:
            time.sleep(5)
            try:
                with _WRITE_LOCK:
                    st = read_state(project)
                    if int(st.get("pid") or 0) != os.getpid():
                        return  # статус сброшен или перехвачен другим процессом
                    if st.get("status") != "running":
                        return
                    _write_state_unlocked(project, st)
            except Exception:  # noqa: BLE001
                pass
    threading.Thread(target=beat, daemon=True).start()


def request_pause(project: str) -> None:
    st = read_state(project)
    st["pause_requested"] = True
    write_state(project, st)


def clear_pause(project: str) -> None:
    st = read_state(project)
    st["pause_requested"] = False
    write_state(project, st)


def is_running(project: str) -> bool:
    st = read_state(project)
    if st.get("status") != "running":
        return False
    pid = st.get("pid")
    if not pid:
        return False
    return _pid_alive(int(pid))


def _pid_alive(pid: int) -> bool:
    """Жив ли процесс. На Windows — точная проверка через WinAPI. Прежний способ
    (подстрока PID в выводе tasklist) давал ложное «жив»: цифры PID совпадали с
    числами в других колонках — отсюда вечный 🟢 при мёртвом процессе."""
    if not pid or int(pid) <= 0:
        return False
    pid = int(pid)
    try:
        if os.name == "nt":
            import ctypes
            STILL_ACTIVE = 259
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        os.kill(pid, 0)
        return True
    except Exception:  # noqa: BLE001
        return False


def _iter_source_files(upload_dir: Path) -> list[Path]:
    from ..ingest.loaders import SUPPORTED_EXT
    files: list[Path] = []
    for p in sorted(upload_dir.rglob("*")):
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXT:
            files.append(p)
    return files


def run_indexing(project: str, cfg: Config | None = None, *, object_type: str | None = None,
                 reindex: bool = False) -> dict:
    """Синхронная индексация (вызывается в фоновом процессе или напрямую).

    Переиндексация безопасна: уже загруженные документы (по doc_sha) пропускаются,
    ID чанков детерминированы, поэтому повторная загрузка не плодит дубли.

    reindex=True — переиндексация «с нуля»: коллекция Qdrant удаляется и все файлы
    обрабатываются заново (нужно при смене режима чанкинга — иначе дедуп по
    doc_sha пропустил бы все файлы, и новый чанкинг не применился бы).
    """
    from ..ingest.sections import classify_filename, detect_version_hint
    from ..ingest.loaders import extract_file
    from ..ingest.dedup import doc_fingerprint
    from ..ingest.chunking import build_chunks

    cfg = cfg or load_config()
    object_type = object_type or cfg.get("object_type", "площадной")
    print(f"[indexer] старт: проект «{project}», pid={os.getpid()}", flush=True)
    paths = project_paths(project)
    upload_dir = paths["uploads"]
    upload_dir.mkdir(parents=True, exist_ok=True)

    # 1) СНАЧАЛА находим файлы и сразу показываем их число (чтобы не висело «0/0»).
    files = _iter_source_files(upload_dir)
    state = read_state(project)
    state.update({
        "status": "running", "pause_requested": False, "pid": os.getpid(),
        "total_files": len(files), "done_files": 0,
        "current_file": "", "message": "Подготовка…",
    })
    state.setdefault("files", {})
    state.setdefault("total_chunks", 0)
    state.setdefault("done_chunks", 0)
    write_state(project, state)

    # Нет файлов — не грузим модель (2 ГБ), сразу понятное сообщение.
    if not files:
        state["status"] = "done"
        state["message"] = ("Нет файлов для индексации. Загрузите файлы проекта "
                            "в Модуле 1 (поддерживаются pdf/docx/xlsx/txt).")
        write_state(project, state)
        return state

    # 2) Загрузка модели эмбеддингов и БД — в защите от ошибок (частая причина
    #    «зависания»: не доустановлены зависимости или первый раз качается модель).
    model_name = str(cfg.get("embedding.model", cfg.get("embeddings.model", "BAAI/bge-m3")))
    reranker_name = str(cfg.get("reranker.model", "BAAI/bge-reranker-v2-m3"))
    # Скачиваем заранее СРАЗУ ОБЕ модели (эмбеддер + reranker), чтобы Модуль 4
    # потом не ждал первой загрузки. Ход скачивания виден в «Журнале индексации».
    for _m in (model_name, reranker_name):
        if _hf_model_cached(_m):
            continue
        state["message"] = f"Скачивание модели {_m} (ход — в «Журнале индексации»)…"
        write_state(project, state)
        print(f"[indexer] скачиваю модель: {_m}", flush=True)
        try:
            from huggingface_hub import snapshot_download
            snapshot_download(_m)
            print(f"[indexer] модель {_m}: скачана ✅", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[indexer] не удалось скачать {_m}: {e}", flush=True)
    if _hf_model_cached(model_name):
        state["message"] = f"Загрузка модели {model_name} с диска (кэш найден)…"
    else:
        state["message"] = (f"Скачивается модель {model_name} (~2.3 ГБ, только при первом "
                            f"запуске). Ход загрузки виден в «Журнале индексации»; пока "
                            f"обновляется «пульс» — процесс жив.")
    write_state(project, state)
    print(f"[indexer] {state['message']}", flush=True)
    try:
        from .embeddings import Embedder
        from .vectorstore import VectorStore
        embedder = Embedder(cfg)
        _ = embedder.dim  # триггерим фактическую загрузку модели здесь, под try
        store = VectorStore(cfg, dim=embedder.dim)
        if reindex:
            dropped = store.drop_collection(project)
            print(f"[indexer] переиндексация с нуля: коллекция "
                  f"{'удалена' if dropped else 'отсутствовала'}", flush=True)
            state["files"] = {}          # снять отметки «done/skipped» — обработать всё заново
            state["done_files"] = 0
            state["total_chunks"] = 0
            state["done_chunks"] = 0
            write_state(project, state)
        store.ensure_collection(project)
        known_shas = store.existing_doc_shas(project)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        err = str(e)
        hint = ""
        if ("float8" in err) or ("PreTrainedModel" in err) or ("Could not import module" in err):
            hint = ("НЕСОВМЕСТИМОСТЬ ВЕРСИЙ: установлен слишком новый transformers для "
                    "torch 2.6. Запустите install.bat ещё раз — он поставит совместимые "
                    "версии (transformers<4.50, sentence-transformers<3.4). ")
        state["status"] = "error"
        state["message"] = (
            f"{hint}Не удалось загрузить модель/векторную БД: {e}. "
            f"Проверьте, что установлены зависимости (запустите install.bat ещё раз — "
            f"возможно, из-за обрыва сети не доустановились sentence-transformers/qdrant), "
            f"и есть интернет для первой загрузки модели bge-m3. "
            f"Подробности — кнопка «Журнал индексации»."
        )
        write_state(project, state)
        return state

    state["message"] = "Модель загружена. Индексация…"
    write_state(project, state)

    try:
        for fpath in files:
            rel = str(fpath.relative_to(upload_dir))
            finfo = state["files"].get(rel, {})
            if finfo.get("status") in ("done", "skipped"):
                state["done_files"] += 1
                write_state(project, state)
                continue

            # Проверка паузы перед каждым файлом.
            if read_state(project).get("pause_requested"):
                state["status"] = "paused"
                state["message"] = "Пауза (возобновляемо)"
                write_state(project, state)
                return state

            state["current_file"] = rel
            state["message"] = f"Обработка: {rel}"
            write_state(project, state)

            try:
                # файл ранее падал (ошибка/жёсткий «Стоп») → могли остаться
                # полузаписанные чанки под тем же файлом: подчищаем перед повтором.
                if finfo.get("status") == "error":
                    try:
                        store.delete_by_file(project, rel)
                    except Exception:  # noqa: BLE001
                        pass
                cls = classify_filename(fpath.name, object_type, top=1)
                section = cls[0]["code"] if cls else "UNKNOWN"
                ver = detect_version_hint(fpath.name) or ""
                pages = extract_file(
                    fpath,
                    ocr=cfg.get("ocr.enabled", True),
                    min_text_chars=cfg.get("ocr.min_text_chars", 200),
                    lang=cfg.get("ocr.lang", "rus+eng"),
                    max_pages=int(cfg.get("ocr.max_pages", 0)),
                )
                sha = doc_fingerprint(pages)
                # подпись содержимого для контентного сравнения версий (пункт 4)
                try:
                    from ..ingest.dedup import content_signature
                    from ..versioning.versions import save_content_sig
                    save_content_sig(project, fpath.name, content_signature(pages))
                except Exception:  # noqa: BLE001
                    pass
                if sha in known_shas:
                    state["files"][rel] = {"status": "skipped", "chunks": 0,
                                           "section": section, "version": ver,
                                           "reason": "дубликат (sha256)"}
                    state["done_files"] += 1
                    write_state(project, state)
                    continue

                chunks = build_chunks(
                    project=project, file_rel=rel, section_code=section,
                    doc_sha=sha, pages=pages,
                    size=cfg.get("chunking.size", 1200),
                    overlap=cfg.get("chunking.overlap", 200),
                    min_chunk=cfg.get("chunking.min_chunk", 80),
                    mode=str(cfg.get("chunking.mode", "char")),
                    target_tokens=int(cfg.get("chunking.target_tokens", 512)),
                    chars_per_token=float(cfg.get("chunking.chars_per_token", 3.2)),
                )
                if chunks:
                    vectors = embedder.embed_documents([c["text"] for c in chunks])
                    try:
                        store.upsert_chunks(project, chunks, vectors)
                    except Exception:
                        # откат частичной записи (OOM/сбой между пачками): иначе
                        # раздел останется недоиндексированным без предупреждения.
                        try:
                            store.delete_by_file(project, rel)
                        except Exception:  # noqa: BLE001
                            pass
                        raise
                    known_shas.add(sha)

                state["files"][rel] = {"status": "done", "chunks": len(chunks),
                                       "section": section, "version": ver}
                state["done_chunks"] = state.get("done_chunks", 0) + len(chunks)
                state["total_chunks"] = state.get("total_chunks", 0) + len(chunks)
                state["done_files"] += 1
                write_state(project, state)
            except Exception as e:  # noqa: BLE001
                state["files"][rel] = {"status": "error", "chunks": 0, "error": str(e)}
                state["done_files"] += 1
                state["message"] = f"Ошибка в {rel}: {e}"
                write_state(project, state)

        state["status"] = "done"
        state["current_file"] = ""
        state["message"] = (f"Индексация завершена: файлов {state.get('done_files', 0)}/"
                            f"{state.get('total_files', 0)}, чанков {state.get('total_chunks', 0)}.")
        write_state(project, state)
        return state
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        state["status"] = "error"
        state["message"] = f"Критическая ошибка: {e}. Подробности — в журнале индексации."
        write_state(project, state)
        return state


def start_background(project: str, *, object_type: str | None = None,
                     reindex: bool = False) -> int:
    """Запускает индексацию отдельным detached-процессом.

    Процесс продолжит работу даже если вкладка Streamlit закрыта. Для
    возобновления после паузы/перезапуска просто вызвать ещё раз — уже
    готовые файлы пропускаются.
    """
    # Защита от гонки: если процесс уже жив, второй запуск затёр бы pid и оставил
    # процесс-сироту (двойная запись в один embedded-Qdrant). Не стартуем второй.
    if not reindex and is_running(project):
        return int(read_state(project).get("pid") or 0)
    clear_pause(project)
    # Пред-скан файлов, чтобы число сразу было видно в интерфейсе (не «0/0»).
    paths = project_paths(project)
    upload_dir = paths["uploads"]; upload_dir.mkdir(parents=True, exist_ok=True)
    files = _iter_source_files(upload_dir)
    if not files:
        st0 = read_state(project)
        st0.update({"status": "done", "total_files": 0, "done_files": 0,
                    "message": "Нет файлов для индексации. Загрузите файлы в Модуле 1."})
        write_state(project, st0)
        return 0

    # Состояние пишем ДО запуска, чтобы дочерний процесс сразу перезаписывал его
    # своими сообщениями (раньше родитель писал ПОСЛЕ и мог затереть их).
    st = read_state(project)
    st.update({
        "status": "running", "pid": 0,
        "total_files": len(files), "done_files": st.get("done_files", 0),
        "current_file": "",
        "message": f"Запуск фонового процесса ({len(files)} файлов)…",
    })
    write_state(project, st)

    lp = log_path(project)
    lp.parent.mkdir(parents=True, exist_ok=True)
    logf = open(lp, "wb")  # №10-2: журнал ОБНУЛЯЕТСЯ при каждом запуске; сюда идут stdout/stderr
    logf.write(f"\n===== {_now()} запуск индексации: {project} =====\n".encode("utf-8"))
    logf.flush()

    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    args = [sys.executable, "-m", "pmoos.index.indexer", "--project", project]
    if object_type:
        args += ["--object-type", object_type]
    if reindex:
        args += ["--reindex"]
    logf.write(f"команда: {' '.join(args)}\nрабочая папка: {APP_ROOT}\n".encode("utf-8"))
    logf.flush()
    kwargs: dict[str, Any] = {"env": env, "cwd": str(APP_ROOT),
                              "stdout": logf, "stderr": logf}
    if os.name == "nt":
        # CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
        kwargs["creationflags"] = 0x00000200 | 0x00000008
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(args, **kwargs)
    except Exception as e:  # noqa: BLE001
        # Запуск дочернего python не удался (антивирус/права/битый venv) — раньше
        # это выглядело как «индексация не запускается» без причины.
        err = f"Не удалось запустить фоновый процесс: {e}"
        logf.write((err + "\n").encode("utf-8")); logf.close()
        st = read_state(project)
        st.update({"status": "error", "pid": 0,
                   "message": err + " Частые причины: антивирус блокирует запуск "
                              "python из этой папки, или окружение venv повреждено "
                              "(повторите install.bat). Команда — в журнале."})
        write_state(project, st)
        return 0
    logf.close()  # дескриптор унаследован дочерним процессом

    # Дописываем pid в АКТУАЛЬНОЕ состояние, не затирая сообщений ребёнка.
    st = read_state(project)
    st["pid"] = proc.pid
    if st.get("status") != "running":
        st["status"] = "running"
    write_state(project, st)
    return proc.pid


def prefetch_models(project: str, models: list[str] | None = None) -> dict:
    """Скачать в кэш HF сразу ВСЕ локальные модели (эмбеддер + reranker), без
    загрузки в память. Замечание пользователя: «надо скачивать сразу все модели»."""
    cfg = load_config()
    if not models:
        models = [
            str(cfg.get("embedding.model", cfg.get("embeddings.model", "BAAI/bge-m3"))),
            str(cfg.get("reranker.model", "BAAI/bge-reranker-v2-m3")),
        ]
    results: dict[str, str] = {}
    state = read_state(project)
    state.update({"status": "running", "pid": os.getpid(),
                  "message": f"Скачивание моделей: всего {len(models)}…"})
    write_state(project, state)
    for i, m in enumerate(models, 1):
        state["message"] = f"Скачивание модели {i}/{len(models)}: {m}…"
        write_state(project, state)
        print(f"[prefetch] {i}/{len(models)}: {m}", flush=True)
        if _hf_model_cached(m):
            print(f"[prefetch] {m}: уже в кэше ✅", flush=True)
            results[m] = "cached"
            continue
        try:
            from huggingface_hub import snapshot_download
            snapshot_download(m)
            results[m] = "downloaded"
            print(f"[prefetch] {m}: скачана ✅", flush=True)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            results[m] = f"ошибка: {e}"
    ok = all(v in ("cached", "downloaded") for v in results.values())
    parts = "; ".join(f"{m.split('/')[-1]} — {'✅' if v in ('cached', 'downloaded') else v}"
                      for m, v in results.items())
    state["status"] = "done" if ok else "error"
    state["message"] = (("Все модели скачаны: " if ok else
                         "Не все модели скачались (подробности в журнале): ") + parts)
    write_state(project, state)
    return results


def start_prefetch_background(project: str) -> int:
    """Скачивание всех моделей отдельным фоновым процессом (журнал и «пульс» — общие)."""
    st = read_state(project)
    st.update({"status": "running", "pid": 0, "current_file": "",
               "message": "Запуск фонового скачивания моделей…"})
    write_state(project, st)
    lp = log_path(project)
    lp.parent.mkdir(parents=True, exist_ok=True)
    logf = open(lp, "wb")  # журнал обнуляется и при предзагрузке моделей
    logf.write(f"\n===== {_now()} скачивание моделей: {project} =====\n".encode("utf-8"))
    logf.flush()
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    args = [sys.executable, "-m", "pmoos.index.indexer", "--project", project, "--prefetch-models"]
    kwargs: dict[str, Any] = {"env": env, "cwd": str(APP_ROOT), "stdout": logf, "stderr": logf}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000200 | 0x00000008
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(args, **kwargs)
    logf.close()
    st = read_state(project)
    st["pid"] = proc.pid
    if st.get("status") != "running":
        st["status"] = "running"
    write_state(project, st)
    return proc.pid


def progress_summary(project: str) -> dict:
    st = read_state(project)
    total_f = st.get("total_files", 0)
    done_f = st.get("done_files", 0)
    pct = (done_f / total_f * 100.0) if total_f else 0.0
    status = st.get("status", "idle")
    running = is_running(project)
    message = st.get("message", "")

    def _age_sec(ts: str) -> float:
        try:
            return max(0.0, (datetime.now() - datetime.fromisoformat(ts)).total_seconds())
        except Exception:  # noqa: BLE001
            return 1e9

    hb_age = _age_sec(st.get("heartbeat") or st.get("updated_at") or "")
    tail = log_tail(project, 12)

    # 1) Статус «running», но процесс МЁРТВ → упал (если бы он завершился сам,
    #    он бы выставил done/error перед выходом). Показываем конец журнала.
    if status == "running" and not running:
        status = "error"
        message = ("Фоновый процесс индексации завершился аварийно. Частые причины: "
                   "не доустановлены зависимости (повторите install.bat) или прервалась "
                   "загрузка модели bge-m3. Конец журнала:\n\n" + (tail or "(журнал пуст)"))
    # 2) Процесс числится живым, но «пульс» не обновлялся > 2 минут → завис.
    elif status == "running" and running and hb_age > 120:
        status = "error"
        message = (f"Процесс индексации не подаёт признаков жизни {int(hb_age)} с "
                   f"(пульс обновляется каждые 5 с). Нажмите «Сбросить статус» и "
                   f"запустите заново. Конец журнала:\n\n" + (tail or "(журнал пуст)"))
    return {
        "status": status,
        "files_done": done_f, "files_total": total_f,
        "chunks_done": st.get("done_chunks", 0),
        "percent": round(pct, 1),
        "current_file": st.get("current_file", ""),
        "message": message,
        "running": running,
        "heartbeat_age": round(hb_age, 1),
    }


def _main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Фоновая индексация STROYPROEKT")
    ap.add_argument("--project", required=True)
    ap.add_argument("--object-type", default=None)
    ap.add_argument("--prefetch-models", action="store_true",
                    help="Скачать все локальные модели (эмбеддер + reranker) и выйти")
    ap.add_argument("--reindex", action="store_true",
                    help="Переиндексация с нуля: удалить коллекцию и обработать всё заново "
                         "(нужно при смене режима чанкинга)")
    a = ap.parse_args()
    print(f"[indexer] процесс запущен, pid={os.getpid()}", flush=True)
    _start_heartbeat(a.project)  # «пульс» — сразу, ещё до тяжёлых импортов
    try:
        if a.prefetch_models:
            prefetch_models(a.project)
        else:
            run_indexing(a.project, object_type=a.object_type, reindex=a.reindex)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        st = read_state(a.project)
        st["status"] = "error"
        st["message"] = f"Аварийное завершение: {e}. Подробности — в журнале индексации."
        write_state(a.project, st)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
