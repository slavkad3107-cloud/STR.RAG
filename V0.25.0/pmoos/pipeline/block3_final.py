"""Блок 3 (МОДУЛЬ 4): окончательная проверка раздела ПМООС в целом.

Смотрит на совокупность ответов/правок и оценивает готовность раздела к
прохождению экспертизы: все ли замечания закрыты, нет ли межразделовых
противоречий, нет ли отменённых нормативов, полнота состава по ПП-87.

Выдаёт итоговый чек-лист рисков и сводку для пользователя.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import Config, load_config
from ..paths import project_paths
from ..core.ai_providers import chat_json
from ..normatives.engine import check_text
from ..ingest.sections import required_sections
from .block1_answers import load_answers
from .consistency import extract_entities

_SYS = (
    "Ты — председатель экспертной комиссии по разделу ПМООС/ООС. Проводишь "
    "финальную проверку готовности раздела к государственной экспертизе по "
    "совокупности подготовленных ответов на замечания. Оцениваешь полноту, "
    "непротиворечивость и нормативную обоснованность. Отвечай строго и по делу."
)

_USER = (
    "Подготовлены ответы на {n} замечаний к разделу ПМООС. Ниже — краткие "
    "сводки по каждому (номер, суть правки, уверенность, недостающие данные):\n\n"
    "{digest}\n\n"
    "СВОДНАЯ АВТО-ПРОВЕРКА НОРМАТИВОВ: {norm}\n"
    "ПОТЕНЦИАЛЬНЫЕ ПРОТИВОРЕЧИЯ СУЩНОСТЕЙ: {contra}\n\n"
    "Верни СТРОГО JSON:\n"
    "{{\n"
    '  "ready": "yes|with_conditions|no",\n'
    '  "summary": "итоговое заключение 3-6 предложений",\n'
    '  "open_issues": ["оставшиеся вопросы/риски"],\n'
    '  "cross_section": "межразделовые несоответствия (или пусто)",\n'
    '  "recommendations": ["приоритетные действия перед сдачей"]\n'
    "}}\nТолько JSON."
)


def _digest(answers: list[dict], limit_chars: int = 220) -> str:
    rows = []
    for a in answers:
        corr = (a.get("correction") or a.get("answer") or "")[:limit_chars]
        rows.append(f"№{a['number']} [{a.get('confidence','?')}]: {corr}"
                    + (f" | не хватает: {a['missing_data']}" if a.get("missing_data") else ""))
    return "\n".join(rows)


def _global_entity_contradictions(answers: list[dict]) -> list[str]:
    """Грубая проверка: одна и та же величина с разными значениями в разных ответах."""
    import re
    val_map: dict[str, set[str]] = {}
    issues = []
    for a in answers:
        text = (a.get("user_answer") or a.get("answer", "")) + " " + a.get("correction", "")
        for m in re.finditer(r"(\d+[.,]?\d*)\s*(г/с|т/год|дБА|мг/м3|мг/м³|м3/сут|га)", text, re.I):
            unit = m.group(2).lower()
            val = m.group(1).replace(",", ".")
            val_map.setdefault(unit, set()).add(val)
    for unit, vals in val_map.items():
        if len(vals) > 3:  # много разных значений одной величины — повод присмотреться
            issues.append(f"Единица «{unit}»: встречается {len(vals)} разных значений в ответах — проверить согласованность.")
    return issues


def run_block3(project: str, cfg: Config | None = None, *, object_type: str | None = None) -> dict[str, Any]:
    cfg = cfg or load_config()
    object_type = object_type or cfg.get("object_type", "площадной")
    data = load_answers(project)
    if not data:
        raise FileNotFoundError("Нет результатов Блока 1.")
    answers = [a for a in data.get("answers", [])
               if a.get("status") in ("accepted", "edited", "proposed")]
    if not answers:
        raise ValueError("Нет принятых ответов для финальной проверки.")

    # сводная проверка нормативов по всем ответам
    all_text = "\n".join((a.get("user_answer") or a.get("answer", "")) + " " + a.get("correction", "")
                         for a in answers)
    norm = check_text(all_text)
    norm_summary = "; ".join(p["recommendation"] for p in norm["problems"]) or "проблем не найдено"
    contra = _global_entity_contradictions(answers)

    msg = [
        {"role": "system", "content": _SYS},
        {"role": "user", "content": _USER.format(
            n=len(answers), digest=_digest(answers),
            norm=norm_summary, contra=("; ".join(contra) or "не выявлено"))},
    ]
    try:
        verdict = chat_json(cfg, msg, expect="object", module="module4", role="review")
    except Exception as e:  # noqa: BLE001
        verdict = {"ready": "", "summary": f"(ИИ недоступен: {e})",
                   "open_issues": [], "recommendations": []}
    verdict = verdict or {}

    # полнота состава ПД (по инвентаризации) — какие обязательные разделы отсутствуют
    missing = _missing_sections(project, object_type)

    out = {
        "project": project, "block": 3, "object_type": object_type,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "remarks_total": len(data.get("answers", [])),
        "remarks_accepted": sum(1 for a in data.get("answers", []) if a.get("status") in ("accepted", "edited")),
        "ready": verdict.get("ready", ""),
        "summary": verdict.get("summary", ""),
        "open_issues": verdict.get("open_issues", []),
        "cross_section": verdict.get("cross_section", ""),
        "recommendations": verdict.get("recommendations", []),
        "normatives": norm,
        "entity_contradictions": contra,
        "missing_required_sections": missing,
    }
    _save(project, out)
    return out


def _missing_sections(project: str, object_type: str) -> list[dict]:
    inv_path = project_paths(project)["inventory"]
    present = set()
    if inv_path.exists():
        try:
            inv = json.loads(inv_path.read_text(encoding="utf-8"))
            present = {i.get("section") for i in inv.get("files", []) if i.get("section")}
        except Exception:
            present = set()
    missing = []
    for s in required_sections(object_type):
        if s["code"] not in present:
            missing.append({"code": s["code"], "name": s.get("name", "")})
    return missing


def _path(project: str) -> Path:
    return project_paths(project)["root"] / "block3_final.json"


def _save(project: str, data: dict) -> Path:
    p = _path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load_block3(project: str) -> dict[str, Any]:
    p = _path(project)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
