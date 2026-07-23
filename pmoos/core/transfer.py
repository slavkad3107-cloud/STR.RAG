"""Перенос базы данных между компьютерами через OneDrive (или любой путь).

Схема «локально работаем — облаком переносим»: живая база (Qdrant + sqlite)
НЕ должна лежать в синхронизируемой папке — OneDrive утаскивает полузаписанные
файлы. Поэтому база всегда живёт в каталоге данных, а сюда встроены две
операции переноса ТОЛЬКО в спокойном состоянии:

    sync_out(dest)  — выгрузить локальную базу в папку переноса (после работы);
    sync_in(dest)   — заменить локальную базу копией из папки переноса.

Уровни защиты (по находкам адверсариального ревью):
  1) отказ, если идёт индексация (живой heartbeat в любом проекте);
  2) замок Qdrant: выгрузка ДЕРЖИТ его всё время копирования (никто не начнёт
     писать в середине снимка); загрузка делает пробу перед стартом;
  3) checkpoint WAL у sqlite-кэшей — копия целостна;
  4) папка выгрузки принимается только пустая/несуществующая или созданная
     нами ранее (наш штамп) — /MIR не сможет стереть случайно указанную
     «D:\\Документы» (наличия чужого projects/ НЕдостаточно);
  5) манифест целостности (_SYNC_MANIFEST.json: число файлов + байты):
     «Забрать» сверяет папку с манифестом и отказывается ставить
     недокачанную OneDrive-копию;
  6) перед «Забрать» локальная база зеркалируется в соседнюю папку
     .pmoos-rag.backup; при сбое копирования — автоматический откат.

venv, pip-cache и models не переносятся (исключаются по АБСОЛЮТНЫМ путям с
обеих сторон — проект с именем «models» внутри projects/ переносится честно):
окружение и модели на каждом компьютере свои.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

from ..paths import data_root, qdrant_dir

EXCLUDE_DIR_NAMES = ("venv", "pip-cache", "models")
INFO_NAME = "_SYNC_INFO.txt"
MANIFEST_NAME = "_SYNC_MANIFEST.json"
META_FILES = (INFO_NAME, MANIFEST_NAME)
SUBDIR = "STR.RAG_BASE"
BACKUP_SUFFIX = ".backup"


def default_dest() -> str:
    """Папка переноса по умолчанию: <OneDrive>/STR.RAG_BASE (если OneDrive есть)."""
    od = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer") or ""
    return str(Path(od) / SUBDIR) if od else ""


def sync_info(dest: str) -> str | None:
    """Строка «кто и когда выгружал» из папки переноса (None, если выгрузки не было)."""
    try:
        p = Path(dest) / INFO_NAME
        return p.read_text(encoding="utf-8", errors="replace").strip() if p.exists() else None
    except OSError:
        return None


def _normalize(dest: str) -> tuple[Path | None, str]:
    """Абсолютный Path папки переноса или (None, причина отказа)."""
    if not dest:
        return None, "Папка переноса не задана (OneDrive не найден — укажите путь)."
    p = Path(dest).expanduser()
    if not p.is_absolute():
        return None, f"Укажите ПОЛНЫЙ путь к папке переноса (сейчас: {dest})."
    p = Path(os.path.normpath(str(p)))
    root = Path(os.path.normpath(str(data_root())))
    if p == root or root in p.parents or p in root.parents:
        return None, ("Папка переноса не может совпадать с каталогом данных "
                      "приложения или лежать внутри него.")
    return p, ""


def _indexing_running() -> str | None:
    from ..index.indexer import read_state  # локальный импорт: не тянуть в тесты
    from ..projects import list_projects

    def _age_sec(ts: str) -> float:
        try:
            return max(0.0, (datetime.now() - datetime.fromisoformat(ts)).total_seconds())
        except (ValueError, TypeError):
            return 1e9  # нечитаемый heartbeat = считаем давно умершим

    for prj in list_projects():
        st = read_state(prj)
        if st.get("status") == "running":
            # поток пульса индексатора пишет heartbeat каждые несколько секунд,
            # даже во время долгих операций; >120 с тишины = заглохший краш
            age = _age_sec(st.get("heartbeat") or st.get("updated_at") or "")
            if age < 120:
                return (f"идёт индексация проекта «{prj}» — дождитесь окончания "
                        f"или нажмите «Стоп» в Модуле 2")
    return None


def _acquire_qdrant():
    """(client, причина_отказа). Держите client открытым на время выгрузки —
    это замок: никто не начнёт писать в базу посреди копирования."""
    try:
        from qdrant_client import QdrantClient
        return QdrantClient(path=str(qdrant_dir())), ""
    except RuntimeError as e:
        if "already accessed" in str(e):
            return None, ("база сейчас занята поиском — подождите пару секунд и "
                          "повторите (или закройте вторую вкладку приложения)")
        raise


def _checkpoint_sqlite() -> None:
    """Влить WAL-журналы sqlite-кэшей в основные файлы (копия будет целостной)."""
    root = data_root()
    for db in list(root.glob("*.sqlite")) + list((root / "emb_cache").glob("*.sqlite")):
        try:
            con = sqlite3.connect(str(db), timeout=3)
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.close()
        except sqlite3.Error:
            pass  # кэш; в худшем случае пересчитается


def _xd_paths(src: Path, dst: Path) -> list[str]:
    """Абсолютные пути исключаемых каталогов С ОБЕИХ СТОРОН: точное совпадение,
    проект с именем «models» глубже по дереву под исключение не попадает."""
    out: list[str] = []
    for name in EXCLUDE_DIR_NAMES:
        out += [str(src / name), str(dst / name)]
    return out


def _robocopy(src: Path, dst: Path) -> tuple[bool, str]:
    """Зеркальное копирование только изменившихся файлов. True = успех."""
    cmd = ["robocopy", str(src), str(dst), "/MIR",
           "/XD", *_xd_paths(src, dst), "/XF", "*.lock", *META_FILES,
           "/R:2", "/W:2", "/NFL", "/NDL", "/NP", "/NJH"]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # robocopy пишет в кодировке КОНСОЛИ (на рус. Windows — CP866/OEM), а не UTF-8:
    # с text=True по умолчанию поток-читатель падал UnicodeDecodeError на кириллице
    # в выводе (имена файлов/ошибки), и res.stdout ломался. Декодируем как OEM с
    # заменой нечитаемого — stdout нужен лишь для диагностического «хвоста».
    res = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="oem", errors="replace", creationflags=flags)
    if res.returncode >= 8:  # 0-7 успех, >=8 ошибка
        tail = (res.stdout or "").strip().splitlines()[-6:]
        return False, "robocopy code %d: %s" % (res.returncode, " | ".join(tail))
    return True, ""


def _measure(root: Path) -> tuple[int, int]:
    """(файлов, байт) в дереве без исключаемых каталогов и наших мета-файлов.
    У OneDrive-плейсхолдеров метаданные (имя+размер) доступны без скачивания."""
    excl = {str(root / n).lower() for n in EXCLUDE_DIR_NAMES}
    n_files = n_bytes = 0
    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if str(Path(cur) / d).lower() not in excl]
        for f in files:
            if f in META_FILES or f.endswith(".lock"):
                continue
            try:
                n_bytes += (Path(cur) / f).stat().st_size
                n_files += 1
            except OSError:
                n_files += 1  # нечитаемый файл всё равно считаем — несовпадение вскроется
    return n_files, n_bytes


def sync_out(dest: str) -> tuple[bool, str]:
    """Выгрузить локальную базу в папку переноса. Возвращает (ok, сообщение)."""
    dpath, err = _normalize(dest)
    if dpath is None:
        return False, err
    src = data_root()
    if not (src / "projects").exists():
        return False, f"Локальная база не найдена: {src}"
    # ЗАЩИТА ОТ /MIR: пишем только в пустую/несуществующую папку либо в папку,
    # созданную нами ранее (наш штамп). Наличие чужого projects/ НЕ признак —
    # иначе случайно указанная личная папка была бы стёрта зеркалированием.
    if dpath.exists():
        ours = (dpath / INFO_NAME).exists() or (dpath / MANIFEST_NAME).exists()
        empty = not any(dpath.iterdir())
        if not ours and not empty:
            return False, (f"Папка {dpath} не пустая и не похожа на папку переноса "
                           f"базы. Укажите пустую папку (например, добавьте к пути "
                           f"\\{SUBDIR}) — иначе её содержимое было бы удалено.")
    reason = _indexing_running()
    if reason:
        return False, f"Сейчас нельзя: {reason}."
    lock, reason = _acquire_qdrant()
    if lock is None:
        return False, f"Сейчас нельзя: {reason}."
    try:
        _checkpoint_sqlite()
        ok, err = _robocopy(src, dpath)
    finally:
        lock.close()
    if not ok:
        return False, f"Копирование не удалось ({err}). Повторите."
    from .. import __version__
    n_files, n_bytes = _measure(dpath)
    (dpath / MANIFEST_NAME).write_text(json.dumps(
        {"files": n_files, "bytes": n_bytes, "version": __version__,
         "host": os.environ.get("COMPUTERNAME", "?"),
         "time": datetime.now().strftime("%d.%m.%Y %H:%M")},
        ensure_ascii=False), encoding="utf-8")
    (dpath / INFO_NAME).write_text(
        f"Выгружено с компьютера {os.environ.get('COMPUTERNAME', '?')} — "
        f"{datetime.now().strftime('%d.%m.%Y %H:%M')} (v{__version__}, "
        f"файлов {n_files})\n", encoding="utf-8")
    return True, (f"База выгружена в {dpath} ({n_files} файлов). Дождитесь зелёной "
                  f"галочки OneDrive, прежде чем забирать её на другом компьютере.")


def _backup_dir() -> Path:
    root = data_root()
    return root.parent / (root.name + BACKUP_SUFFIX)


def sync_in(dest: str) -> tuple[bool, str]:
    """Заменить локальную базу копией из папки переноса. Возвращает (ok, сообщение)."""
    dpath, err = _normalize(dest)
    if dpath is None:
        return False, err
    if not (dpath / MANIFEST_NAME).exists():
        return False, (f"В папке {dpath} нет манифеста выгрузки — это не папка "
                       f"переноса базы (или выгрузка с другого компьютера не "
                       f"завершилась). Сначала нажмите «Выгрузить» там.")
    # МАНИФЕСТ: облако обязано совпасть по числу файлов и байтам — иначе OneDrive
    # ещё не докачал папку, и /MIR поставил бы недокачанный снимок, удалив живое.
    try:
        man = json.loads((dpath / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "Манифест выгрузки не читается — повторите выгрузку на той стороне."
    n_files, n_bytes = _measure(dpath)
    if n_files != int(man.get("files", -1)) or n_bytes != int(man.get("bytes", -1)):
        return False, (f"OneDrive ещё не досинхронизировал папку переноса: в ней "
                       f"{n_files} файлов ({n_bytes:,} байт), а выгружено "
                       f"{man.get('files')} ({man.get('bytes'):,}). Дождитесь "
                       f"зелёной галочки OneDrive и повторите.")
    reason = _indexing_running()
    if reason:
        return False, f"Сейчас нельзя: {reason}."
    lock, reason = _acquire_qdrant()  # только проба: держать нельзя — /MIR не сможет писать
    if lock is None:
        return False, f"Сейчас нельзя: {reason}."
    lock.close()
    _checkpoint_sqlite()
    # СТРАХОВКА: свежее зеркало локальной базы рядом; при сбое — автоткат
    bdir = _backup_dir()
    ok, err = _robocopy(data_root(), bdir)
    if not ok:
        return False, f"Не удалось сделать страховочную копию ({err}) — перенос отменён."
    ok, err = _robocopy(dpath, data_root())
    if not ok:
        r_ok, r_err = _robocopy(bdir, data_root())
        if r_ok:
            return False, (f"Копирование из облака сорвалось ({err}) — локальная "
                           f"база АВТОМАТИЧЕСКИ ВОССТАНОВЛЕНА из страховочной "
                           f"копии. Проверьте OneDrive и повторите.")
        return False, (f"Копирование сорвалось ({err}), и откат тоже ({r_err}). "
                       f"Страховочная копия лежит в {bdir} — не удаляйте её и "
                       f"напишите в поддержку/ассистенту.")
    extra = ""
    from .. import __version__
    if str(man.get("version", "")) not in ("", __version__):
        extra = (f" Внимание: база выгружена версией v{man.get('version')}, у вас "
                 f"v{__version__} — обновите приложение на обоих компьютерах "
                 f"(git pull), если появятся странности.")
    return True, (f"Локальная база заменена копией из {dpath} "
                  f"(выгружал {man.get('host', '?')} {man.get('time', '')}). "
                  f"Страховочная копия прежней базы: {bdir}.{extra}")
