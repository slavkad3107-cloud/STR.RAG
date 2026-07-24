"""Экспорт обучающих троек (Anchor / Positive / Negative) — запрос пользователя.

Опционально для дообучения эмбеддера (contrastive learning) в будущем:
  Anchor   — текст замечания;
  Positive — чанк ПД, где реально найден ответ (топ-источник принятого ответа);
  Negative — похожий по словам, но нерелевантный чанк (источник ДРУГОГО замечания).

Также копит общий датасет по всем проектам в data_root()/training/ для будущей
системы автонаписания разделов. Формат — JSONL (по одной тройке на строку).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..paths import project_paths, data_root


def _answers(project: str) -> dict:
    from ..pipeline.block1_answers import load_answers
    return load_answers(project)


def _best_snippet(ans: dict) -> str:
    srcs = ans.get("sources") or []
    if not srcs:
        return ""
    # самый релевантный источник (первый — наибольший score)
    return (srcs[0].get("snippet") or "").strip()


def build_triples(project: str, *, accepted_only: bool = True) -> list[dict]:
    data = _answers(project)
    answers = data.get("answers", [])
    if accepted_only:
        pool = [a for a in answers if a.get("status") in ("accepted", "edited")]
        if not pool:  # если ещё ничего не принято — берём все с источниками
            pool = [a for a in answers if a.get("sources")]
    else:
        pool = [a for a in answers if a.get("sources")]

    triples: list[dict] = []
    n = len(pool)
    for i, a in enumerate(pool):
        anchor = (a.get("remark") or "").strip()
        positive = _best_snippet(a)
        if not anchor or not positive:
            continue
        # hard negative: топ-источник другого замечания, НЕ входящий в файлы
        # текущих источников (находка аудита: my_files вычислялся, но фильтр не
        # применялся — негативом мог стать сниппет из того же документа)
        my_files = {s.get("file") for s in (a.get("sources") or [])}
        negative = ""
        for j in range(1, n):
            other = pool[(i + j) % n]
            ofile = ((other.get("sources") or [{}])[0]).get("file")
            neg = _best_snippet(other)
            if neg and neg != positive and (not ofile or ofile not in my_files):
                negative = neg
                break
        triples.append({
            "anchor": anchor,
            "positive": positive,
            "negative": negative,
            "project": project,
            "remark_number": a.get("number"),
            "section": (a.get("sources") or [{}])[0].get("section", ""),
        })
    return triples


def export_triples(project: str, *, accepted_only: bool = True,
                   also_global: bool = True) -> dict[str, Path | int]:
    """Записать тройки в out/training_triples.jsonl (+ в общий датасет). 
    Возвращает {'path':…, 'count':N, 'global':…}."""
    triples = build_triples(project, accepted_only=accepted_only)
    out_dir = project_paths(project)["out"]
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "training_triples.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for t in triples:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")

    result: dict[str, Path | int] = {"path": path, "count": len(triples)}

    if also_global and triples:
        gdir = data_root() / "training"
        gdir.mkdir(parents=True, exist_ok=True)
        gpath = gdir / "triples_all.jsonl"
        stamp = datetime.now().isoformat(timespec="seconds")
        with gpath.open("a", encoding="utf-8") as f:
            for t in triples:
                t = dict(t, exported_at=stamp)
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
        result["global"] = gpath
    return result
