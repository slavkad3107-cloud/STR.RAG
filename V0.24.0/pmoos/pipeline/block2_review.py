"""Блок 2 (МОДУЛЬ 4): проверка принятых исправлений, расчётов, ссылок и нормативов.

Берёт ответы, принятые/отредактированные в Блоке 1, и:
  * проверяет актуальность нормативных ссылок (normatives.engine);
  * проверяет согласованность сущностей с источниками (consistency);
  * просит ИИ оценить корректность правки, расчётов и ссылок на документы,
    предложить уточнения;
  * для каждого пункта показывает каскад затронутых разделов.

Финальное принятие — снова за пользователем.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import Config, load_config
from ..paths import project_paths
from ..core.ai_providers import batch_chat
from ..core.json_utils import extract_json_safe
from ..normatives.engine import check_text
from .block1_answers import load_answers
from .consistency import extract_entities

_SYS = (
    "Ты — рецензент-эксперт государственной экспертизы по разделу ПМООС. "
    "Тебе дают замечание, предложенный ответ/правку и контекст. Оцени: "
    "корректность по существу, наличие и достаточность расчётов, правильность "
    "ссылок на разделы и нормативы, риски для прохождения экспертизы. Будь строг, "
    "но конкретен. Не выдумывай несуществующие нормативы."
)

_USER = (
    "ЗАМЕЧАНИЕ №{num}:\n«{remark}»\n\n"
    "ПРЕДЛОЖЕННЫЙ ОТВЕТ/ПРАВКА:\n{answer}\n\n"
    "АВТО-ПРОВЕРКА НОРМАТИВОВ (предварительно):\n{norm}\n\n"
    "Верни СТРОГО JSON:\n"
    "{{\n"
    '  "verdict": "ok|needs_work|reject",\n'
    '  "calc_check": "оценка расчётной части (что проверить/пересчитать)",\n'
    '  "norm_check": "оценка ссылок на нормативы (актуальность/корректность)",\n'
    '  "improvements": "конкретные улучшения формулировки ответа",\n'
    '  "risks": "риски для экспертизы"\n'
    "}}\nТолько JSON."
)


def _norm_summary(check: dict) -> str:
    parts = []
    if check.get("problems"):
        for p in check["problems"]:
            parts.append(f"- {p['ref']}: {p['recommendation']}")
    if check.get("unknown"):
        parts.append("Не в реестре (проверить вручную): " + ", ".join(check["unknown"][:10]))
    return "\n".join(parts) if parts else "Явных проблем не найдено."


def _accepted(data: dict) -> list[dict]:
    out = []
    for a in data.get("answers", []):
        if a.get("status") in ("accepted", "edited", "proposed"):
            out.append(a)
    return out


def run_block2(project: str, cfg: Config | None = None, *, progress=None) -> dict[str, Any]:
    cfg = cfg or load_config()
    data = load_answers(project)
    if not data:
        raise FileNotFoundError("Нет результатов Блока 1. Сначала выполните поиск ответов.")
    items = _accepted(data)
    if not items:
        raise ValueError("Нет принятых ответов для проверки (примите ответы в Блоке 1).")

    # авто-проверка нормативов до запросов к ИИ
    jobs, prechecks = [], []
    for a in items:
        text = (a.get("user_answer") or a.get("answer", "")) + "\n" + a.get("correction", "")
        ncheck = check_text(text)
        prechecks.append(ncheck)
        jobs.append([
            {"role": "system", "content": _SYS},
            {"role": "user", "content": _USER.format(num=a["number"], remark=a["remark"],
                                                      answer=text.strip() or "(пусто)",
                                                      norm=_norm_summary(ncheck))},
        ])

    if progress:
        progress(0, len(items), "Рецензия исправлений ИИ…")
    results = batch_chat(cfg, jobs, processor=lambda r: extract_json_safe(r, expect="object") or {},
                         module="module4", role="review", json_mode=True)

    reviews = []
    for a, ncheck, res in zip(items, prechecks, results):
        rev = res.get("result") if res.get("ok") else {}
        rev = rev or {}
        reviews.append({
            "number": a["number"],
            "remark": a["remark"],
            "answer_reviewed": a.get("user_answer") or a.get("answer", ""),
            "normatives": ncheck,
            "verdict": rev.get("verdict", ""),
            "calc_check": rev.get("calc_check", ""),
            "norm_check": rev.get("norm_check", ""),
            "improvements": rev.get("improvements", ""),
            "risks": rev.get("risks", ""),
            "entities": extract_entities(a.get("answer", "")).to_dict(),
            "status": "proposed",
            "error": res.get("error"),
        })
        if progress:
            progress(len(reviews), len(items), f"Замечание {a['number']}")

    out = {"project": project, "block": 2, "count": len(reviews),
           "generated_at": datetime.now().isoformat(timespec="seconds"),
           "reviews": reviews}
    _save(project, out)
    return out


def _block2_path(project: str) -> Path:
    return project_paths(project)["root"] / "block2_review.json"


def _save(project: str, data: dict) -> Path:
    p = _block2_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load_block2(project: str) -> dict[str, Any]:
    p = _block2_path(project)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
