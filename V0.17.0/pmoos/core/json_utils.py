"""Надёжное извлечение JSON из ответа ИИ.

Исправляет ошибку «Не удалось извлечь сбалансированный JSON»: модели часто
оборачивают JSON в ```json … ```, добавляют пояснения до/после, ставят висячие
запятые, используют «умные» кавычки и т.п. Здесь — устойчивый парсер, который:
  * срезает markdown-ограждения и преамбулы;
  * находит СБАЛАНСИРОВАННЫЙ фрагмент { … } или [ … ] с учётом строк/экранирования;
  * пытается json.loads, при неудаче — лёгкий ремонт (запятые, кавычки) и повтор;
  * умеет вернуть несколько объектов и «вытащить» массив верхнего уровня.
"""
from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
_SMART = {
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u00ab": '"', "\u00bb": '"',
    "\u2018": "'", "\u2019": "'", "\uff02": '"',
}


def _strip_fences(text: str) -> str:
    m = _FENCE.search(text)
    return m.group(1) if m else text


def _normalize(text: str) -> str:
    for a, b in _SMART.items():
        text = text.replace(a, b)
    return text


def _balanced_span(text: str, open_ch: str, close_ch: str) -> str | None:
    """Возвращает первый сбалансированный фрагмент от open_ch до парного close_ch,
    учитывая строки и экранирование."""
    start = text.find(open_ch)
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _repair(s: str) -> str:
    s = _normalize(s)
    # убрать висячие запятые перед } или ]
    s = re.sub(r",\s*([}\]])", r"\1", s)
    # одинарные кавычки ключей -> двойные (грубо, для простых случаев)
    s = re.sub(r"([{,]\s*)'([^']+?)'(\s*:)", r'\1"\2"\3', s)
    return s


def extract_json(text: str, *, expect: str = "auto") -> Any:
    """Достаёт JSON-объект или массив из произвольного текста модели.

    expect: 'object' | 'array' | 'auto'. Бросает ValueError, если ничего нет.
    """
    if text is None:
        raise ValueError("Пустой ответ модели")
    raw = _strip_fences(str(text)).strip()

    # 1) самый частый случай — весь ответ уже валидный JSON
    for candidate in (raw, _repair(raw)):
        try:
            return json.loads(candidate)
        except Exception:
            pass

    # 2) ищем сбалансированный фрагмент нужного типа
    order: tuple[tuple[str, str], ...]
    if expect == "object":
        order = (("{", "}"),)
    elif expect == "array":
        order = (("[", "]"),)
    else:
        # берём то, что встречается раньше
        oi = raw.find("{")
        ai = raw.find("[")
        if ai != -1 and (oi == -1 or ai < oi):
            order = (("[", "]"), ("{", "}"))
        else:
            order = (("{", "}"), ("[", "]"))

    for op, cl in order:
        span = _balanced_span(raw, op, cl)
        if not span:
            continue
        for candidate in (span, _repair(span)):
            try:
                return json.loads(candidate)
            except Exception:
                continue

    raise ValueError("Не удалось извлечь сбалансированный JSON из ответа модели")


def extract_json_safe(text: str, default: Any = None, *, expect: str = "auto") -> Any:
    """Как extract_json, но возвращает default вместо исключения."""
    try:
        return extract_json(text, expect=expect)
    except Exception:
        return default


def extract_all_objects(text: str) -> list[dict]:
    """Достаёт ВСЕ объекты {…} верхнего уровня (полезно для построчных ответов)."""
    raw = _strip_fences(str(text or ""))
    out: list[dict] = []
    i = 0
    while True:
        j = raw.find("{", i)
        if j == -1:
            break
        span = _balanced_span(raw[j:], "{", "}")
        if not span:
            break
        for candidate in (span, _repair(span)):
            try:
                obj = json.loads(candidate)
                if isinstance(obj, dict):
                    out.append(obj)
                break
            except Exception:
                continue
        i = j + len(span)
    return out
