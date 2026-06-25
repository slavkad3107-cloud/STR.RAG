"""Расширение запроса (query expansion) под поиск ответов на замечания.

Замечание эксперта часто сформулировано канцелярским языком и не совпадает
лексически с текстом разделов ПД. Генерируем несколько перефразировок и
ключевых терминов, чтобы повысить полноту гибридного поиска.
"""
from __future__ import annotations

from .. import config as _cfg
from ..core.ai_providers import chat_json, batch_chat, LLMError
from ..core.json_utils import extract_json_safe

_SYS = (
    "Ты — инженер-эколог, эксперт по проектной документации (ПМООС/ООС) "
    "и государственной экспертизе по Постановлению Правительства РФ №87. "
    "Твоя задача — переформулировать замечание эксперта в несколько поисковых "
    "запросов к технической документации (разделы ТКР, ПОС, ИЭИ, ПМООС и др.)."
)

_PROMPT = (
    "Замечание эксперта:\n«{remark}»\n\n"
    "Сгенерируй {n} коротких поисковых запросов (на русском), которые помогут "
    "найти в проектной документации данные для ответа: используй синонимы, "
    "нормативную терминологию, названия величин, разделов и расчётов. "
    "Верни СТРОГО JSON-массив строк без пояснений."
)


def _messages(remark_text: str, n: int) -> list[dict]:
    return [
        {"role": "system", "content": _SYS},
        {"role": "user", "content": _PROMPT.format(remark=remark_text.strip(), n=n)},
    ]


def _merge(remark_text: str, extra_raw, n: int) -> list[str]:
    """Исходный запрос + перефразировки, дедуп с сохранением порядка."""
    base = [remark_text.strip()]
    extra = [str(x).strip() for x in (extra_raw or []) if str(x).strip()]
    seen = {base[0].lower()}
    for q in extra:
        if q.lower() not in seen:
            base.append(q)
            seen.add(q.lower())
    return base[: n + 1]


def expand_query(remark_text: str, cfg: _cfg.Config, *, n: int = 3, module: str = "module4") -> list[str]:
    """Возвращает список запросов: исходный + перефразировки.

    При недоступности ИИ (нет ключа и т.п.) деградирует мягко: возвращает
    только исходный текст, не роняя пайплайн.
    """
    base = [remark_text.strip()]
    if n <= 0:
        return base
    try:
        data = chat_json(cfg, _messages(remark_text, n), expect="array",
                         module=module, role="expand")
        return _merge(remark_text, data, n)
    except (LLMError, Exception):
        return base


def expand_query_batch(remark_texts: list[str], cfg: _cfg.Config, *, n: int = 3,
                       module: str = "module4") -> list[list[str]]:
    """ПАРАЛЛЕЛЬНОЕ расширение для списка замечаний (оптимизация М4).

    Раньше расширение шло по одному замечанию последовательно — для 75 замечаний
    это 75 блокирующих вызовов LLM ещё до генерации ответов. Здесь все запросы
    уходят разом через batch_chat (пул потоков ai.concurrency). Мягкая деградация:
    при ошибке по конкретному замечанию возвращаем только его исходный текст.
    """
    if n <= 0 or not remark_texts:
        return [[t.strip()] for t in remark_texts]
    jobs = [_messages(t, n) for t in remark_texts]
    try:
        results = batch_chat(
            cfg, jobs, module=module, role="expand", json_mode=True,
            processor=lambda txt: extract_json_safe(txt, default=[], expect="array"),
        )
    except Exception:  # noqa: BLE001
        return [[t.strip()] for t in remark_texts]
    out: list[list[str]] = []
    for t, res in zip(remark_texts, results):
        data = res.get("result") if res.get("ok") else None
        out.append(_merge(t, data, n))
    return out
