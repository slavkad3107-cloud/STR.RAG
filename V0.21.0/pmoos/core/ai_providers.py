"""Единый интерфейс к разным провайдерам ИИ.

Поддержка: deepseek, openai (gpt), gemini, anthropic (claude), ollama (локально).
Тяжёлые SDK импортируются лениво (внутри функций) — чтобы приложение стартовало
быстро и не падало, если какой-то SDK не установлен.

Возможности:
  * единый chat(...) -> str;
  * chat_json(...) -> dict/list (с JSON-режимом там, где провайдер умеет, и
    устойчивым извлечением JSON в остальных случаях);
  * дисковый кэш ответов (sqlite) — экономит токены при повторных прогонах;
  * batch_chat(...) — параллельные запросы (для десятков замечаний разом).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Iterable

from ..config import Config, ENV_KEYS
from ..paths import data_root
from .json_utils import extract_json

Message = dict[str, str]
_LOCK = threading.Lock()


# --- дисковый кэш ответов ---------------------------------------------------
# Одно переиспользуемое соединение на процесс (оптимизация): раньше каждый
# get/put открывал и закрывал sqlite — под batch_chat (десятки параллельных
# запросов) это лишние connect/close на каждый вызов. WAL + busy_timeout делают
# одно общее соединение безопасным; запись/чтение сериализуем общим _LOCK.
_CACHE_CON: sqlite3.Connection | None = None


def _cache_db() -> sqlite3.Connection:
    global _CACHE_CON
    if _CACHE_CON is None:
        p = data_root() / "llm_cache.sqlite"
        con = sqlite3.connect(str(p), check_same_thread=False)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            con.execute("PRAGMA busy_timeout=5000")
        except Exception:  # noqa: BLE001
            pass
        con.execute("CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, v TEXT)")
        _CACHE_CON = con
    return _CACHE_CON


def _cache_key(provider: str, model: str, messages: list[Message], **params) -> str:
    blob = json.dumps(
        {"p": provider, "m": model, "msgs": messages, "params": params},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> str | None:
    try:
        with _LOCK:
            row = _cache_db().execute("SELECT v FROM cache WHERE k=?", (key,)).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _cache_put(key: str, value: str) -> None:
    try:
        with _LOCK:
            con = _cache_db()
            con.execute("INSERT OR REPLACE INTO cache (k, v) VALUES (?, ?)", (key, value))
            con.commit()
    except Exception:
        pass


class LLMError(RuntimeError):
    pass


# --- вызовы провайдеров -----------------------------------------------------
def _openai_like(base_url: str, api_key: str, model: str, messages: list[Message],
                 temperature: float, max_tokens: int, json_mode: bool) -> str:
    """DeepSeek и OpenAI используют один и тот же протокол (openai SDK)."""
    try:
        from openai import OpenAI
    except Exception as e:  # pragma: no cover
        raise LLMError("Не установлен пакет openai (pip install openai)") from e
    client = OpenAI(api_key=api_key, base_url=base_url or None)
    kwargs: dict[str, Any] = {
        "model": model, "messages": messages,
        "temperature": temperature, "max_tokens": max_tokens,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


def _gemini(api_key: str, model: str, messages: list[Message],
            temperature: float, max_tokens: int, json_mode: bool) -> str:
    try:
        import google.generativeai as genai
    except Exception as e:  # pragma: no cover
        raise LLMError("Не установлен google-generativeai") from e
    genai.configure(api_key=api_key)
    sys_txt = "\n".join(m["content"] for m in messages if m["role"] == "system")
    gen_cfg: dict[str, Any] = {"temperature": temperature, "max_output_tokens": max_tokens}
    if json_mode:
        gen_cfg["response_mime_type"] = "application/json"
    gm = genai.GenerativeModel(model, system_instruction=sys_txt or None,
                               generation_config=gen_cfg)
    history = []
    for m in messages:
        if m["role"] == "system":
            continue
        history.append({"role": "user" if m["role"] == "user" else "model",
                        "parts": [m["content"]]})
    resp = gm.generate_content(history)
    return getattr(resp, "text", "") or ""


def _anthropic(api_key: str, model: str, messages: list[Message],
               temperature: float, max_tokens: int, json_mode: bool) -> str:
    try:
        import anthropic
    except Exception as e:  # pragma: no cover
        raise LLMError("Не установлен пакет anthropic") from e
    client = anthropic.Anthropic(api_key=api_key)
    sys_txt = "\n".join(m["content"] for m in messages if m["role"] == "system")
    conv = [{"role": m["role"], "content": m["content"]}
            for m in messages if m["role"] in ("user", "assistant")]
    if json_mode:
        sys_txt = (sys_txt + "\nОтвечай СТРОГО валидным JSON без пояснений.").strip()
    resp = client.messages.create(model=model, system=sys_txt or None,
                                  messages=conv, temperature=temperature,
                                  max_tokens=max_tokens)
    return "".join(getattr(b, "text", "") for b in resp.content) or ""


def _ollama(base_url: str, model: str, messages: list[Message],
            temperature: float, max_tokens: int, json_mode: bool) -> str:
    import requests
    url = (base_url or "http://localhost:11434").rstrip("/") + "/api/chat"
    payload: dict[str, Any] = {
        "model": model, "messages": messages, "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens,
                    "num_ctx": 8192},
    }
    if json_mode:
        payload["format"] = "json"
    r = requests.post(url, json=payload, timeout=600)
    r.raise_for_status()
    data = r.json()
    return (data.get("message") or {}).get("content", "") or ""


# --- публичный API ----------------------------------------------------------
def _chat_once(cfg: Config, messages: list[Message], *, provider: str, role: str,
               model: str | None, temperature: float, max_tokens: int,
               json_mode: bool, use_cache: bool) -> str:
    """Одна попытка вызова конкретного провайдера (без fallback)."""
    model = model or cfg.model_for(provider, role)
    json_mode = json_mode and cfg.supports_json_mode(provider)
    if not model:
        raise LLMError(f"Не задана модель для провайдера '{provider}' (роль '{role}')")
    if not cfg.has_key(provider):
        envs = ", ".join(ENV_KEYS.get(provider, ()))
        raise LLMError(
            f"Нет ключа API для провайдера '{provider}'. "
            f"Задайте переменную окружения ({envs}) в .env или в настройках ИИ."
        )

    key = _cache_key(provider, model, messages, t=temperature, mt=max_tokens, j=json_mode)
    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            return cached

    api_key = cfg.api_key(provider)
    base = cfg.base_url(provider)
    if provider in ("deepseek", "openai", "kimi", "mistral"):
        out = _openai_like(base, api_key, model, messages, temperature, max_tokens, json_mode)
    elif provider == "gemini":
        out = _gemini(api_key, model, messages, temperature, max_tokens, json_mode)
    elif provider == "anthropic":
        out = _anthropic(api_key, model, messages, temperature, max_tokens, json_mode)
    elif provider == "ollama":
        out = _ollama(base, model, messages, temperature, max_tokens, json_mode)
    else:
        raise LLMError(f"Неизвестный провайдер: {provider}")

    if use_cache and out:
        _cache_put(key, out)
    return out


def chat(cfg: Config, messages: list[Message], *, module: str | None = None,
         role: str = "answer", provider: str | None = None, model: str | None = None,
         temperature: float | None = None, max_tokens: int | None = None,
         json_mode: bool = False, use_cache: bool | None = None) -> str:
    """Главная точка вызова ИИ. Провайдер/модель определяются автоматически
    под модуль (model_for/resolve_provider), если не заданы явно.

    Fallback-цепочка (v0.21): если основной провайдер упал (сеть/лимиты/API),
    ОДИН повтор через резервный из ai.fallback_provider (пусто = выключено).
    Модель резервного берётся его же настройкой той же роли. Явно заданный
    аргументом provider тоже страхуется (fallback — не для случая «нет ключа
    у самого fallback» и не когда fallback совпадает с основным)."""
    provider = provider or cfg.resolve_provider(module)
    temperature = cfg.get("ai.temperature", 0.1) if temperature is None else temperature
    max_tokens = cfg.get("ai.max_tokens", 4096) if max_tokens is None else max_tokens
    use_cache = cfg.get("ai.use_cache", True) if use_cache is None else use_cache

    try:
        return _chat_once(cfg, messages, provider=provider, role=role, model=model,
                          temperature=temperature, max_tokens=max_tokens,
                          json_mode=json_mode, use_cache=use_cache)
    except Exception as e:  # noqa: BLE001
        fb = str(cfg.get("ai.fallback_provider", "") or "").strip()
        if not fb or fb == provider or not cfg.has_key(fb):
            raise
        print(f"[ai] провайдер '{provider}' недоступен ({e}) — повтор через '{fb}'",
              flush=True)
        # модель основного к резервному не применима — резервный берёт свою (role)
        return _chat_once(cfg, messages, provider=fb, role=role, model=None,
                          temperature=temperature, max_tokens=max_tokens,
                          json_mode=json_mode, use_cache=use_cache)


def chat_json(cfg: Config, messages: list[Message], *, expect: str = "auto", **kw) -> Any:
    """Вызов ИИ с разбором JSON. Сначала пробуем JSON-режим, затем устойчивый
    парсер. Это и есть лечение «не удалось извлечь сбалансированный JSON».

    Если распарсить не удалось — ОДИН повтор с жёстким требованием «верни ТОЛЬКО
    JSON» (без кэша, чтобы не закрепить плохой ответ). Повтор включается
    ai.json_repair_retry (по умолчанию True)."""
    txt = chat(cfg, messages, json_mode=True, **kw)
    try:
        return extract_json(txt, expect=expect)
    except Exception:
        if not cfg.get("ai.json_repair_retry", True):
            raise
        schema_hint = {"object": "JSON-объект", "array": "JSON-массив"}.get(expect, "валидный JSON")
        retry_msgs = list(messages)
        # Эхо прошлого ответа добавляем ТОЛЬКО если последняя роль не assistant —
        # иначе у anthropic/gemini получилось бы два assistant подряд (ошибка
        # чередования ролей). Текущие вызывающие заканчиваются на user.
        last_role = next((m.get("role") for m in reversed(retry_msgs) if m.get("content")), None)
        if last_role != "assistant":
            retry_msgs.append({"role": "assistant", "content": (txt or "")[:2000]})
        retry_msgs.append({"role": "user", "content": (
            f"Твой предыдущий ответ не распарсился как {schema_hint}. "
            f"Верни ТОЛЬКО {schema_hint} по требуемой схеме — без markdown, "
            f"без ```-ограждений и без каких-либо пояснений.")})
        kw2 = dict(kw)
        kw2["use_cache"] = False  # не кэшируем повторный запрос
        txt2 = chat(cfg, retry_msgs, json_mode=True, **kw2)
        return extract_json(txt2, expect=expect)


def batch_chat(cfg: Config, jobs: Iterable[list[Message]], *, processor: Callable[[str], Any] | None = None,
               **kw) -> list[Any]:
    """Параллельные запросы к ИИ (для десятков замечаний). Возвращает список
    результатов В ТОМ ЖЕ ПОРЯДКЕ, что и входные задания."""
    jobs = list(jobs)
    workers = max(1, int(cfg.get("ai.concurrency", 4)))
    results: list[Any] = [None] * len(jobs)

    def _run(idx: int, msgs: list[Message]):
        try:
            txt = chat(cfg, msgs, **kw)
            return idx, (processor(txt) if processor else txt), None
        except Exception as e:  # noqa: BLE001
            return idx, None, str(e)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_run, i, j) for i, j in enumerate(jobs)]
        for f in as_completed(futs):
            idx, val, err = f.result()
            results[idx] = {"ok": err is None, "result": val, "error": err}
    return results
