"""Память экспертизы (Historical Expertise Memory) — обучение на прошлых проектах.

Главная цель пользователя: «обучение работе над ошибками и формирование ответов
на замечания», накопление знаний по 100–500 проектам. Оба ревью-ИИ независимо
назвали это самым полезным шагом (полезнее ещё одной LLM).

Как работает:
  • когда инженер ПРИНИМАЕТ/ПРАВИТ ответ (Модуль 4), пара «замечание → принятый
    ответ» попадает в общую базу data_root()/memory/expertise.jsonl;
  • при генерации новых ответов (Блок 1) система ищет ПОХОЖИЕ принятые замечания
    из прошлых проектов и подмешивает их в промпт как few-shot примеры (in-context
    learning) — это переиспользует удачные формулировки эксперта.

Поиск похожести — лексический (Jaccard по нормализованным токенам + бонус за
редкие доменные термины). Не требует GPU/эмбеддера, работает всегда; при желании
позже можно заменить на векторный поиск. База растёт сама по мере работы.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from ..paths import data_root

_STOP = {
    "и", "в", "во", "на", "по", "с", "со", "к", "о", "об", "от", "до", "за", "для",
    "что", "как", "это", "при", "не", "ни", "или", "а", "но", "же", "бы", "ли",
    "раздел", "ответ", "замечание", "необходимо", "следует", "просьба", "прошу",
    "пункт", "также", "быть", "должен", "должна", "должно", "был", "была",
}


def _store_path() -> Path:
    d = data_root() / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d / "expertise.jsonl"


def _tokens(text: str) -> set[str]:
    toks = re.findall(r"[\wа-яё]+", (text or "").lower())
    return {t for t in toks if len(t) >= 4 and t not in _STOP}


# --- кэш записей KB (оптимизация) ------------------------------------------
# Раньше весь expertise.jsonl читался и токенизировался заново на КАЖДОЕ
# замечание (fewshot_block в цикле) — O(замечания × записи_KB), что становится
# квадратичным при цели 100–500 проектов. Теперь records и предвычисленные
# token-наборы кэшируются и перечитываются только при изменении файла.
_RECORDS_LOCK = threading.Lock()
_RECORDS_CACHE: dict[str, Any] = {"sig": object(), "records": [], "tokens": []}


def _file_sig(p: Path):
    try:
        st = p.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _read_raw() -> list[dict]:
    """Чтение записей напрямую с диска (без кэша) — для веток с перезаписью."""
    p = _store_path()
    if not p.exists():
        return []
    out = []
    for line in p.open("r", encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return out


def _load_cached() -> tuple[list[dict], list[set]]:
    """(records, предвычисленные токены замечаний); перечитывается лишь при
    изменении файла (mtime/size). Возвращаемые списки — НЕ мутировать."""
    p = _store_path()
    sig = _file_sig(p)
    with _RECORDS_LOCK:
        if _RECORDS_CACHE["sig"] == sig:
            return _RECORDS_CACHE["records"], _RECORDS_CACHE["tokens"]
    records = _read_raw()
    tokens = [_tokens(r.get("remark", "")) for r in records]
    with _RECORDS_LOCK:
        _RECORDS_CACHE.update(sig=sig, records=records, tokens=tokens)
    return records, tokens


def _invalidate_cache() -> None:
    """Форсировать перечитывание кэша при следующем _load_cached.

    Страховка от грубой гранулярности mtime (FAT/exFAT/сетевые ФС): не полагаемся
    только на (mtime, size) — после записи в record_one явно сбрасываем сигнатуру."""
    with _RECORDS_LOCK:
        _RECORDS_CACHE["sig"] = object()
        _VEC_CACHE["sig"] = object()


# --- векторный поиск похожих замечаний (v0.21) -------------------------------
# Лексический Jaccard плохо ловит перефразированные замечания. Семантический
# поиск через bge-m3 (модель уже загружена ретривером М4, эмбеддинги идут через
# общий дисковый кэш — повторные прогоны почти бесплатны). Мягкая деградация:
# любая ошибка → прежний лексический путь.
_VEC_CACHE: dict[str, Any] = {"sig": object(), "vecs": None}


def _kb_vectors(records: list[dict], cfg=None):
    """Эмбеддинги замечаний базы; пересчёт только при изменении базы."""
    from ..config import load_config
    from ..index.embeddings import Embedder
    with _RECORDS_LOCK:
        sig = _RECORDS_CACHE["sig"]
        if _VEC_CACHE["sig"] == sig and _VEC_CACHE["vecs"] is not None:
            return _VEC_CACHE["vecs"]
    emb = Embedder(cfg or load_config())
    vecs = emb.embed([r.get("remark", "") for r in records])  # нормированные
    with _RECORDS_LOCK:
        _VEC_CACHE.update(sig=sig, vecs=vecs)
    return vecs


def _semantic_similar(remark_text: str, *, k: int, exclude_project: str | None,
                      min_sim: float, cfg=None) -> list[dict] | None:
    """Топ-k по косинусной близости bge-m3. None → семантика выключена."""
    if cfg is None:  # cfg передаётся сверху (similar_past) — без повторного чтения YAML
        from ..config import load_config
        cfg = load_config()
    if not cfg.get("memory.semantic", True):
        return None
    records, _ = _load_cached()
    if not records:
        return []
    from ..index.embeddings import Embedder
    vecs = _kb_vectors(records, cfg)
    qv = Embedder(cfg).embed([remark_text])[0]
    sims = vecs @ qv  # векторы нормированы → dot = cosine
    rt_low = (remark_text or "").strip().lower()
    scored: list[tuple[float, dict]] = []
    for r, s in zip(records, sims):
        if exclude_project and r.get("project") == exclude_project:
            continue
        if r.get("remark", "").strip().lower() == rt_low:
            continue
        if float(s) >= min_sim:
            scored.append((float(s), r))
    scored.sort(key=lambda t: t[0], reverse=True)
    out = []
    for score, r in scored[:k]:
        rr = dict(r)
        rr["score"] = round(score, 3)
        out.append(rr)
    return out


def kb_size() -> int:
    return len(_load_cached()[0])


def _iter_records() -> list[dict]:
    # обратная совместимость: отдаём кэшированные записи (только для чтения)
    return _load_cached()[0]


def _seen_keys() -> set[str]:
    return {f"{r.get('project')}|{r.get('number')}" for r in _load_cached()[0]}


def record_one(*, remark: str, answer: str, correction: str = "", section: str = "",
               project: str = "", number: str | int = "") -> bool:
    """Добавить одну пару (замечание→ответ) в базу. Дедуп по (project, number)."""
    remark = (remark or "").strip()
    answer = (answer or "").strip()
    if not remark or not answer:
        return False
    key = f"{project}|{number}"
    if key in _seen_keys():
        # обновим существующую запись (перезапись answer) — простая реализация.
        # Читаем СВЕЖИЕ записи с диска (не кэш), т.к. ниже мутируем и перезаписываем.
        records = _read_raw()
        changed = False
        for r in records:
            if f"{r.get('project')}|{r.get('number')}" == key:
                r["answer"] = answer
                r["correction"] = correction
                r["section"] = section
                r["ts"] = datetime.now().isoformat(timespec="seconds")
                changed = True
        if changed:
            with _store_path().open("w", encoding="utf-8") as f:
                for r in records:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            _invalidate_cache()
        return changed
    rec = {
        "remark": remark, "answer": answer, "correction": correction,
        "section": section, "project": project, "number": str(number),
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    with _store_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    _invalidate_cache()
    return True


def record_accepted(project: str) -> int:
    """Собрать в базу все принятые/правленые ответы проекта. Возвращает число
    добавленных/обновлённых записей."""
    from ..pipeline.block1_answers import load_answers
    data = load_answers(project)
    n = 0
    for a in data.get("answers", []):
        if a.get("status") in ("accepted", "edited"):
            ans = (a.get("user_answer") or a.get("answer") or "").strip()
            sec = ""
            srcs = a.get("sources") or []
            if srcs:
                sec = srcs[0].get("section", "")
            if record_one(remark=a.get("remark", ""), answer=ans,
                          correction=a.get("correction", ""), section=sec,
                          project=project, number=a.get("number", "")):
                n += 1
    # параллельно обновляем накопительный граф знаний по проектам (пункт 1B)
    try:
        from ..graph.knowledge import update_from_project
        update_from_project(project)
    except Exception:  # noqa: BLE001
        pass
    return n


def similar_past(remark_text: str, *, k: int = 2, exclude_project: str | None = None,
                 min_score: float = 0.12) -> list[dict]:
    """Топ-k похожих принятых замечаний из прошлых проектов.

    Сначала семантический поиск (bge-m3, конфиг memory.semantic, порог
    memory.min_sim); при выключенной семантике или любой ошибке — прежний
    лексический Jaccard (работает без GPU/моделей)."""
    try:
        from ..config import load_config
        cfg = load_config()  # один раз на вызов; ниже передаём внутрь
        min_sim = float(cfg.get("memory.min_sim", 0.45))
        sem = _semantic_similar(remark_text, k=k, exclude_project=exclude_project,
                                min_sim=min_sim, cfg=cfg)
        if sem is not None:
            return sem
    except Exception:  # noqa: BLE001
        pass  # мягкая деградация на лексический путь
    q = _tokens(remark_text)
    if not q:
        return []
    rt_low = (remark_text or "").strip().lower()
    records, tokens = _load_cached()  # токены замечаний предвычислены один раз
    scored: list[tuple[float, dict]] = []
    for r, d in zip(records, tokens):
        if exclude_project and r.get("project") == exclude_project:
            continue
        if r.get("remark", "").strip().lower() == rt_low:
            continue  # не подсказывать самим собой
        if not d:
            continue
        inter = q & d
        if not inter:
            continue
        jacc = len(inter) / len(q | d)
        if jacc >= min_score:
            scored.append((jacc, r))
    scored.sort(key=lambda t: t[0], reverse=True)
    out = []
    for score, r in scored[:k]:
        rr = dict(r)
        rr["score"] = round(score, 3)
        out.append(rr)
    return out


def fewshot_block(remark_text: str, *, k: int = 2, exclude_project: str | None = None,
                  max_answer_chars: int = 500) -> str:
    """Сформировать текст few-shot примеров для промпта. Пусто, если нет похожих."""
    ex = similar_past(remark_text, k=k, exclude_project=exclude_project)
    if not ex:
        return ""
    lines = ["ПРИМЕРЫ ПРИНЯТЫХ ОТВЕТОВ ПО ПОХОЖИМ ЗАМЕЧАНИЯМ (из прошлых проектов, "
             "используй как образец стиля и подхода, но опирайся на данные ТЕКУЩЕГО проекта):"]
    for i, r in enumerate(ex, 1):
        ans = r.get("answer", "")[:max_answer_chars]
        lines.append(f"[Пример {i}] Замечание: {r.get('remark','')[:300]}\n"
                     f"Принятый ответ: {ans}")
    return "\n".join(lines)
