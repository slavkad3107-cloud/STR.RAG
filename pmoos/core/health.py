"""Проверка провайдеров ИИ и АВТОВЫБОР лучшего (просьба пользователя:
«при запуске все модели проверяются на работу/лимит, выбирается лучший —
сначала локальные, потом большие бесплатные, и только потом DeepSeek»).

Как работает:
  • probe_all() параллельно опрашивает каждый провайдер, у которого есть ключ:
    GET /models (или /api/tags у Ollama) — дёшево, без расхода токенов;
  • у рабочих провайдеров сразу известен СПИСОК моделей → выбираем лучшую по
    таблице предпочтений (PREFERRED), а не по зашитому имени, которое могло
    устареть (реальный случай: DeepSeek переименовал модели, и всё падало 400);
  • результат кэшируется в <данные>/provider_health.json;
  • auto_select() ставит лучшего рабочего провайдера/модель в конфиг.

Ранжирование (tier, меньше = раньше):
  0 — ollama: локально, бесплатно, приватно;
  1 — cerebras / groq: бесплатные и очень быстрые;
  2 — gemini / openrouter / cohere / mistral: бесплатные с лимитами;
  3 — deepseek: платный, но дешёвый и проверенный на этом домене;
  4 — openai / anthropic: платные дорогие — только если больше нечем.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from .. import __version__
from ..paths import data_root

# Groq и Cerebras стоят за Cloudflare, который отбивает подпись urllib по
# умолчанию («Python-urllib/3.x») ошибкой 403 «error code: 1010» — это бан по
# подписи клиента, а НЕ блок по стране: 10.09.2026 тот же GET /models у Groq
# через тот же VPN с подписью urllib давал 403, со своей — 200. Из-за этого
# проверка считала Groq «недоступным из вашей страны» и не выбирала никогда.
USER_AGENT = f"StroyRAG/{__version__}"

# Порядок по ТЗ 27.07.2026: «сначала большие бесплатные, потом локальные и уже
# потом DeepSeek (только он платный)». Замеры это подтверждают: облачная
# mistral-large отвечает на замечание экспертизы заметно лучше локальной 7B.
TIERS: dict[str, int] = {
    "mistral": 0, "cohere": 0, "gemini": 0, "openrouter": 0,
    "cerebras": 0, "groq": 0,
    # v0.54: облако ollama.com через локальную Ollama, Z.ai GLM, Cloudflare
    # Workers AI — тоже бесплатные облачные
    "ollama_cloud": 0, "zai": 0, "cloudflare": 0,
    "ollama": 1,
    "deepseek": 2,
    "openai": 3, "anthropic": 3, "kimi": 3,
}

TIER_RU = {0: "большой бесплатный", 1: "локальный (бесплатно)",
           2: "платный (дешёвый)", 3: "платный дорогой"}

# Локальная модель идёт ПЕРВОЙ только если она достаточно крупная: qwen2.5:7b
# на замечаниях госэкспертизы заметно слабее облачных 70B+/flash-моделей, а
# ответы эксперта важнее экономии. Мелкая локальная остаётся в цепочке, но
# ПОСЛЕ бесплатных облачных (tier 2.5).
MIN_LOCAL_B = 13.0

# Качество внутри одного tier (меньше = лучше): латентность — плохой критерий,
# порядок задаём осознанно под русские инженерные тексты.
QUALITY: dict[str, int] = {
    # Порядок ЗАМЕРЕН на реальном замечании экспертизы (v0.34):
    # mistral-large 2858 символов и лучшая структура; cohere command-a 2230;
    # gemini через совместимый эндпоинт обрывает ответ (12 токенов из 300);
    # openrouter на бесплатных моделях вернул пустоту.
    # v0.54 (10.09.2026): mistral-large в бесплатный тариф Mistral больше НЕ
    # входит (бесплатны только ministral-14b/8b/3b и codestral), поэтому первым
    # идёт замеренный Cohere command-a, за ним бесплатная ministral-14b и
    # облачная gpt-oss-120b (Ollama Cloud, Groq). Новые облачные на замечаниях
    # не замерены — поднимать после замера (М7 / eval_golden).
    "cohere": 0,
    "mistral": 1, "ollama_cloud": 1,
    "groq": 2, "cerebras": 2,
    "zai": 3, "openrouter": 3,
    "cloudflare": 4,
    "gemini": 5,
    "deepseek": 0, "anthropic": 0, "openai": 1, "kimi": 2, "ollama": 0,
}


def _model_size_b(name: str) -> float:
    """Размер модели в млрд параметров из имени («qwen2.5:7b» → 7.0)."""
    m = re.search(r"[:\-_ ](\d{1,3}(?:\.\d)?)\s*b\b", (name or "").lower())
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return 0.0
    m2 = re.search(r"(\d{2,3})b", (name or "").lower())
    return float(m2.group(1)) if m2 else 0.0

# Предпочтения моделей внутри провайдера: список регулярок, первая совпавшая —
# лучшая. Домен — русские инженерные тексты, поэтому крупные instruct-модели.
PREFERRED: dict[str, list[str]] = {
    "ollama":     [r"qwen3.*(30|32|14)b", r"qwen2\.5.*(14|32)b", r"qwen", r"llama3\.[13]",
                   r"gemma", r"mistral", r"."],
    "cerebras":   [r"gpt-oss-120b", r"qwen-3", r"gemma-4", r"."],
    # 10.09.2026 у Groq остались gpt-oss-120b/20b и qwen3.x-27b; kimi-k2 и
    # llama-3.3-70b сняты (404)
    "groq":       [r"gpt-oss-120b", r"gpt-oss-20b", r"qwen", r"."],
    # у OpenRouter мелкие бесплатные модели молчат — сначала крупные
    "openrouter": [r"nemotron-3-super.*:free", r"gemma-4-31b.*:free",
                   r"deepseek.*(v3|chat).*:free", r"qwen.*235b.*:free",
                   r"llama-3\.3-70b.*:free", r"qwen.*(32|30)b.*:free",
                   r"nemotron.*:free", r"mistral.*:free", r":free", r"."],
    # ВАЖНО: «gemini-2.5-flash» новым ключам отдаёт 404, поэтому первым идёт
    # проверенный алиас gemini-flash-latest (v0.34)
    "gemini":     [r"gemini-flash-latest", r"gemini-pro-latest", r"gemini-2\.0-flash",
                   r"flash", r"."],
    "cohere":     [r"command-a", r"command-r-plus", r"command-r", r"."],
    # бесплатный тариф Mistral (10.09.2026, по заголовкам лимитов): только
    # ministral-14b/8b/3b и codestral; large/medium/small — 0 запросов/мин
    "mistral":    [r"ministral-14b", r"codestral", r"ministral-8b", r"ministral-3b",
                   r"mistral-large", r"mistral-medium", r"mistral-small", r"."],
    "deepseek":   [r"v4-pro", r"reasoner", r"v4-flash", r"chat", r"."],
    "openai":     [r"gpt-4\.1$", r"gpt-4o$", r"gpt-4\.1-mini", r"gpt-4o-mini", r"."],
    "anthropic":  [r"sonnet", r"haiku", r"."],
    "kimi":       [r"128k", r"32k", r"."],
    "ollama_cloud": [r"gpt-oss:120b", r"gemma4", r"nemotron-3-super", r"nemotron-3-nano",
                     r"."],
    "zai":        [r"glm-4\.7-flash$", r"glm-4\.5-flash$", r"flash$", r"."],
    "cloudflare": [r"gpt-oss-120b", r"llama-3\.3-70b", r"."],
}

# OpenAI-совместимые: (базовый URL, путь списка моделей)
_OPENAI_LIKE = {
    "gemini":     "https://generativelanguage.googleapis.com/v1beta/openai",
    "deepseek":   "https://api.deepseek.com",
    "openai":     "https://api.openai.com/v1",
    "mistral":    "https://api.mistral.ai/v1",
    "kimi":       "https://api.moonshot.cn/v1",
    "groq":       "https://api.groq.com/openai/v1",
    "cerebras":   "https://api.cerebras.ai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "cohere":     "https://api.cohere.ai/compatibility/v1",
    "zai":        "https://api.z.ai/api/paas/v4",
}

# бесплатные облачные модели ollama.com тарифа пользователя (10.09.2026)
OLLAMA_CLOUD_FREE = ["gpt-oss:120b-cloud", "gemma4:31b-cloud",
                     "nemotron-3-super:cloud", "nemotron-3-nano:30b-cloud"]
# у Cloudflare Workers AI нет OpenAI-списка моделей — кандидаты задаём сами
CLOUDFLARE_MODELS = ["@cf/openai/gpt-oss-120b",
                     "@cf/meta/llama-3.3-70b-instruct-fp8-fast"]
# бесплатные модели Z.ai: в его /models их НЕТ (там только платные glm-4.5…5.3),
# и проверка по списку попадала на платные — «нет средств» (10.09.2026)
ZAI_FREE_MODELS = ["glm-4.7-flash", "glm-4.5-flash"]


def cloudflare_base(api_key: str) -> tuple[str, str]:
    """(базовый URL, токен) Cloudflare Workers AI. ID аккаунта входит в адрес:
    из CLOUDFLARE_ACCOUNT_ID или из ключа вида «ID_аккаунта:токен»."""
    import os
    acc, sep, token = (api_key or "").partition(":")
    if not sep:
        acc, token = "", api_key or ""
    acc = acc or (os.environ.get("CLOUDFLARE_ACCOUNT_ID") or "").strip()
    if not acc:
        return "", token
    return f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/v1", token


def explain_http(code: int, body: str, headers: Any = None) -> str:
    """Человеческая причина отказа провайдера — по коду, телу и заголовкам.

    Разбор 10.09.2026: «403 — недоступно из вашей страны» писалось на всё
    подряд, а это разные вещи с разным лечением."""
    low = (body or "").lower()
    try:
        zero = str((headers or {}).get("x-ratelimit-limit-req-minute", "")) == "0"
    except Exception:  # noqa: BLE001
        zero = False
    if code == 429 and zero:
        # Mistral: модель вне бесплатного тарифа отвечает обычным 429
        return "модель не входит в бесплатный тариф (лимит 0 запросов/мин)"
    if "error code: 1010" in low:
        return "Cloudflare отклонил подпись клиента (1010) — это не блок по стране"
    if ("location is not supported" in low or "unsupported_country" in low
            or "security policy" in low or "error code: 1009" in low
            or (code == 403 and '"forbidden"' in low)):
        return "недоступно из вашей страны (нужен VPN)"
    if "expired" in low:
        return "срок ключа истёк"
    if code == 402 or "payment required" in low:
        return "нужна оплата (бесплатный доступ закрыт)"
    if "tier_not_allowed" in low or "not available in your subscription" in low:
        return "модель недоступна на вашем тарифе"
    if "credit balance" in low or "insufficient" in low or "billing" in low:
        return "нет средств на балансе провайдера"
    if "overloaded" in low:
        return "сервис перегружен (бесплатный тариф) — позже заработает"
    if "quota" in low or "rate limit" in low or "resource_exhausted" in low:
        return "исчерпан лимит запросов"
    return {401: "ключ недействителен",
            403: "403: доступ запрещён (ключ отозван или нет прав)",
            404: "модель недоступна этому ключу",
            429: "исчерпан лимит запросов"}.get(code, f"HTTP {code}")


def _get_json(url: str, headers: dict[str, str], timeout: float = 12.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _try_generate(provider: str, base: str, api_key: str, model: str,
                  timeout: float = 12.0, _retry: bool = True) -> tuple[bool, str]:
    """РЕАЛЬНАЯ мини-генерация (1-5 токенов): единственная честная проверка.

    Список моделей врёт: реальный случай — gemini-2.5-flash есть в /models, но
    отдаёт 404 «no longer available to new users», а gemini-2.0-flash — 429
    «лимит». Без этой проверки автовыбор ставил бы мёртвую модель, и все 75
    ответов падали бы (ровно то, что и произошло у пользователя с DeepSeek)."""
    try:
        if provider in ("ollama", "ollama_cloud"):
            body = json.dumps({"model": model, "stream": False,
                               "messages": [{"role": "user", "content": "ок"}],
                               "options": {"num_predict": 1}}).encode()
            req = urllib.request.Request(f"{base.rstrip('/')}/api/chat", data=body,
                                         headers={"Content-Type": "application/json"})
        elif provider == "anthropic":
            body = json.dumps({"model": model, "max_tokens": 5,
                               "messages": [{"role": "user", "content": "ок"}]}).encode()
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages", data=body,
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                         "Content-Type": "application/json"})
        else:
            body = json.dumps({"model": model, "max_tokens": 5,
                               "messages": [{"role": "user", "content": "ок"}]}).encode()
            req = urllib.request.Request(
                f"{base.rstrip('/')}/chat/completions", data=body,
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"})
        req.add_header("User-Agent", USER_AGENT)
        with urllib.request.urlopen(req, timeout=timeout):
            return True, ""
    except urllib.error.HTTPError as e:
        try:  # тело ответа объясняет причину точнее кода (баланс, квота, регион)
            body = e.read()[:400].decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        if provider == "ollama_cloud" and e.code == 404 and _retry:
            # ярлыка облачной модели ещё нет в Ollama — скачать (килобайты) и повторить
            try:
                pull = urllib.request.Request(
                    f"{base.rstrip('/')}/api/pull",
                    data=json.dumps({"model": model, "stream": False}).encode(),
                    headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
                with urllib.request.urlopen(pull, timeout=60):
                    pass
            except Exception as pe:  # noqa: BLE001
                code = getattr(pe, "code", 0)
                return False, ("Ollama не вошла в аккаунт ollama.com (ollama signin)"
                               if code in (401, 403) else f"ярлык не скачался ({pe})")
            return _try_generate(provider, base, api_key, model, timeout, _retry=False)
        if provider == "ollama_cloud" and e.code in (401, 403):
            return False, "Ollama не вошла в аккаунт ollama.com (ollama signin)"
        return False, explain_http(e.code, body, e.headers)
    except Exception as e:  # noqa: BLE001
        # таймаут — это «медлит/перегружен», а не «нет связи» (Z.ai на бесплатном
        # тарифе 10.09.2026 не укладывался в 12 с при живом соединении)
        if isinstance(e, TimeoutError) or "timed out" in str(e).lower():
            return False, f"не ответил за {int(timeout)} с (перегружен или медленный)"
        return False, f"нет связи ({type(e).__name__})"


def _models_from(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        for key in ("data", "models"):
            items = payload.get(key)
            if isinstance(items, list):
                out = []
                for m in items:
                    if isinstance(m, dict):
                        out.append(str(m.get("id") or m.get("name") or ""))
                    else:
                        out.append(str(m))
                return [x for x in out if x]
    return []


# НЕ чат-модели: эмбеддеры/реранкеры/картинки/аудио — в кандидаты не берём
# (в списке Ollama рядом с чат-моделями лежит bge-m3 — наш эмбеддер!)
_NOT_CHAT = re.compile(
    r"(bge|embed|rerank|whisper|tts|audio|image|vision-only|dall|sora|veo|imagen|"
    r"moderation|guard|nomic|minilm|clip)", re.I)


def rank_models(provider: str, models: list[str], top: int = 4) -> list[str]:
    """Кандидаты по убыванию предпочтения (для проверки живой генерацией)."""
    models = [m for m in (models or []) if not _NOT_CHAT.search(m)]
    out: list[str] = []
    for pat in PREFERRED.get(provider, [r"."]):
        rx = re.compile(pat, re.I)
        for m in sorted([x for x in models if rx.search(x)], key=len):
            if m not in out:
                out.append(m)
            if len(out) >= top:
                return out
    for m in models:                      # добор, если предпочтения не сошлись
        if m not in out:
            out.append(m)
        if len(out) >= top:
            break
    return out


def pick_model(provider: str, models: list[str]) -> str:
    """Лучшая модель провайдера по таблице предпочтений."""
    cands = rank_models(provider, models, top=1)
    return cands[0] if cands else ""


def probe_ollama(hosts: list[str], cloud: bool = False) -> dict:
    """Ollama: перебираем адреса (свой компьютер, затем второй по имени).

    cloud=False — только локальные модели, cloud=True — только облачные ярлыки
    ollama.com (…-cloud): это разные провайдеры, данные облачных уходят в сеть."""
    from .ollama_utils import is_ollama_cloud
    for host in hosts:
        host = host.strip().rstrip("/")
        if not host:
            continue
        t0 = time.time()
        try:
            data = _get_json(f"{host}/api/tags", {}, timeout=4.0)
            models = [m for m in _models_from(data) if is_ollama_cloud(m) == cloud]
            if models or cloud:
                return {"ok": bool(models), "models": models, "host": host,
                        "ms": int((time.time() - t0) * 1000)}
        except Exception:  # noqa: BLE001 — просто следующий адрес
            continue
    return {"ok": False, "error": "Ollama не отвечает (запустите `ollama serve`)"}


def probe_provider(provider: str, api_key: str, base_url: str = "",
                   ollama_hosts: list[str] | None = None) -> dict:
    """Один провайдер: жив ли ключ, какие модели, не упёрлись ли в лимит."""
    if provider == "ollama":
        return probe_ollama(ollama_hosts or ["http://localhost:11434"])
    if provider == "ollama_cloud":
        # облако ollama.com через ЛОКАЛЬНУЮ Ollama: ключ не нужен (в аккаунт
        # входит сама Ollama); кандидаты — уже подключённые облачные ярлыки, а
        # если их нет — бесплатные модели тарифа (ярлык скачается сам)
        res = probe_ollama(ollama_hosts or ["http://localhost:11434"], cloud=True)
        if res.get("host") and not res.get("models"):
            res.update(ok=True, models=list(OLLAMA_CLOUD_FREE), error="")
        return res
    if not api_key:
        return {"ok": False, "error": "нет ключа"}
    if provider == "cloudflare":
        if not cloudflare_base(api_key)[0]:
            return {"ok": False, "error": "нет ID аккаунта Cloudflare (CLOUDFLARE_ACCOUNT_ID)"}
        return {"ok": True, "models": list(CLOUDFLARE_MODELS), "ms": 0, "error": ""}
    if provider == "zai":
        return {"ok": True, "models": list(ZAI_FREE_MODELS), "ms": 0, "error": ""}
    t0 = time.time()
    try:
        if provider == "gemini":
            data = _get_json(
                "https://generativelanguage.googleapis.com/v1beta/models?key=" + api_key, {})
            models = [str(m.get("name", "")).replace("models/", "")
                      for m in (data.get("models") or [])]
        elif provider == "anthropic":
            data = _get_json("https://api.anthropic.com/v1/models",
                             {"x-api-key": api_key, "anthropic-version": "2023-06-01"})
            models = _models_from(data)
        else:
            base = (base_url or _OPENAI_LIKE.get(provider, "")).rstrip("/")
            if not base:
                return {"ok": False, "error": "неизвестный провайдер"}
            data = _get_json(f"{base}/models", {"Authorization": f"Bearer {api_key}"})
            models = _models_from(data)
        return {"ok": bool(models), "models": models,
                "ms": int((time.time() - t0) * 1000),
                "error": "" if models else "пустой список моделей"}
    except urllib.error.HTTPError as e:
        code = e.code
        try:
            body = e.read()[:400].decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        msg = explain_http(code, body, e.headers)
        return {"ok": False, "error": msg, "http": code,
                "limited": code == 429 and "бесплатный тариф" not in msg}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"нет связи ({type(e).__name__})"}


def _health_path():
    return data_root() / "provider_health.json"


def read_health(max_age_h: float = 0.0) -> dict:
    """Кэш проверок. max_age_h > 0 — вернуть только СВЕЖИЙ кэш (иначе {})."""
    p = _health_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if max_age_h > 0:
        try:
            import os
            age_h = (time.time() - os.path.getmtime(p)) / 3600.0
            if age_h > max_age_h:
                return {}
        except OSError:
            return {}
    return data


_PROBE_LOCK = __import__("threading").Lock()


def probe_all_async(cfg, providers: list[str] | None = None) -> bool:
    """Опросить провайдеров В ФОНОВОМ ПОТОКЕ — интерфейс не ждёт.

    Синхронный опрос на старте держал приложение «немым» до 100 с (находка
    аудита): у зависшего провайдера уходило 4 кандидата × таймаут. Теперь
    результат просто появляется в кэше, а интерфейс рисуется сразу."""
    import threading
    if not _PROBE_LOCK.acquire(blocking=False):
        return False                       # опрос уже идёт

    def run() -> None:
        try:
            probe_all(cfg, providers)
        except Exception:  # noqa: BLE001
            pass
        finally:
            _PROBE_LOCK.release()

    threading.Thread(target=run, daemon=True).start()
    return True


def probe_all(cfg, providers: list[str] | None = None) -> dict:
    """Опросить всех параллельно. Возвращает {провайдер: результат} + сохраняет."""
    from ..config import ENV_KEYS  # локальный импорт: избегаем цикла
    import os
    provs = providers or list(TIERS)
    hosts = [h for h in (os.environ.get("OLLAMA_HOSTS")
                         or os.environ.get("OLLAMA_HOST")
                         or "http://localhost:11434").split(",") if h.strip()]

    def one(p: str) -> tuple[str, dict]:
        key = ""
        try:
            key = cfg.api_key(p) or ""
        except Exception:  # noqa: BLE001
            key = ""
        base = ""
        try:
            base = cfg.base_url(p) or ""
        except Exception:  # noqa: BLE001
            base = ""
        res = probe_provider(p, key, base, hosts)
        res["tier"] = TIERS.get(p, 9)
        res["checked_at"] = datetime.now().isoformat(timespec="seconds")
        res["best_model"] = ""
        if res.get("ok") and p == "ollama":
            # Ollama НЕ проверяем генерацией: холодная модель грузится в память
            # десятки секунд (таймаут ≠ поломка), а раз модель есть в списке —
            # она рабочая. Первый ответ будет медленным, дальше быстро.
            res["best_model"] = pick_model(p, res.get("models") or [])
            if not res["best_model"]:
                res["ok"] = False
                res["error"] = "в Ollama нет чат-моделей (есть только эмбеддер)"
        elif res.get("ok"):
            # ЖИВАЯ ПРОВЕРКА: перебираем кандидатов, пока какой-то реально не
            # ответит (список моделей врёт — см. _try_generate)
            gen_base = (base or _OPENAI_LIKE.get(p, ""))
            gen_key = key
            if p == "ollama_cloud":
                gen_base = res.get("host") or gen_base or "http://localhost:11434"
            elif p == "cloudflare":
                gen_base, gen_key = cloudflare_base(key)
            last = ""
            # top=2: каждый лишний кандидат — ещё один таймаут в бюджете старта
            for cand in rank_models(p, res.get("models") or [], top=2):
                ok_gen, err = _try_generate(p, gen_base or "", gen_key, cand)
                if ok_gen:
                    res["best_model"] = cand
                    break
                last = err
            if not res["best_model"]:
                res["ok"] = False
                res["error"] = last or "модели не отвечают"
                res["limited"] = "лимит" in (last or "")
        return p, res

    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for p, res in ex.map(one, provs):
            out[p] = res
    try:
        _health_path().write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    except OSError:
        pass
    return out


def effective_tier(provider: str, res: dict) -> float:
    """Tier по ТЗ. Крупная локальная модель (≥13B) поднимается ближе к
    облачным бесплатным: она бесплатна, приватна и по качеству сопоставима.

    Берём tier из ТЕКУЩЕЙ таблицы, а не из кэша: после смены приоритетов
    старый provider_health.json давал перевёрнутый порядок."""
    tier = float(TIERS.get(provider, res.get("tier", 9)))
    if provider == "ollama" and _model_size_b(res.get("best_model", "")) >= MIN_LOCAL_B:
        return 0.5      # сразу после облачных бесплатных
    return tier


def rank_working(health: dict) -> list[tuple[str, dict]]:
    """Рабочие провайдеры: сначала дешевизна (tier), внутри — качество."""
    ok = [(p, r) for p, r in health.items() if r.get("ok") and r.get("best_model")]
    return sorted(ok, key=lambda kv: (effective_tier(kv[0], kv[1]),
                                      QUALITY.get(kv[0], 5),
                                      kv[1].get("ms", 10**6)))


def auto_select(cfg, health: dict | None = None, *, force: bool = False) -> tuple[str, str]:
    """Поставить лучшего РАБОЧЕГО провайдера, НЕ ломая ручные настройки.

    Правки по находке аудита (high): раньше при каждом запуске стирались
    выбранные пользователем провайдеры модулей (М1/М3/М4) и затирались дешёвые
    модели вспомогательных ролей — необратимо, прямо в config.yaml. Теперь:
      • если текущий провайдер по умолчанию РАБОТАЕТ — не трогаем ничего;
      • меняем только answer/review (extract/expand — лишь если пусты);
      • переопределение модуля снимаем ТОЛЬКО если его провайдер не работает.
    force=True — явное действие пользователя (кнопка «Выбрать лучший»)."""
    health = health or read_health() or probe_all(cfg)
    ranked = rank_working(health)
    if not ranked:
        return "", ""
    cur = cfg.default_provider()
    cur_ok = bool((health.get(cur) or {}).get("ok"))
    provider, res = ranked[0]
    model = res.get("best_model", "")

    # ВСЕГДА снимаем переопределения модулей, чей провайдер НЕ РАБОТАЕТ: иначе
    # М4 молча упирался в мёртвого провайдера (реальный случай: default=ollama
    # жив, а module4=cerebras отдаёт 404 — все ответы падали, хотя приложение
    # считало, что всё в порядке).
    _cleaned = []
    for mod in ("module1", "module3", "module4"):
        ov = cfg.get(f"ai.modules.{mod}.provider")
        if ov and not (health.get(ov) or {}).get("ok"):
            cfg.set(f"ai.modules.{mod}", {})
            _cleaned.append(f"{mod}:{ov}")
    if _cleaned:
        cfg.save()
        print(f"[health] сняты нерабочие настройки модулей: {', '.join(_cleaned)}",
              flush=True)

    if cur_ok and not force:
        # текущий жив — остальное не трогаем (уважаем выбор пользователя)
        return cur, cfg.model_for(cur, "answer") or model
    cfg.set("ai.default_provider", provider)
    for role in ("answer", "review"):
        cfg.set(f"ai.providers.{provider}.{role}", model)
    for role in ("extract", "expand"):
        # force (кнопка «Выбрать лучшую») — ставим проверенную модель и сюда:
        # снятый провайдером слаг во вспомогательной роли давал 404 на каждом
        # расширении запроса (07.09.2026, openrouter qwen3-32b:free)
        if force or not cfg.model_for(provider, role):
            cfg.set(f"ai.providers.{provider}.{role}", model)
    if provider == "ollama":  # генерация должна идти на ТОТ ЖЕ адрес, где нашли
        host = res.get("host")
        if host:
            cfg.set("ai.providers.ollama.base_url", host)
    cfg.save()
    return provider, model


def fallback_order(cfg, health: dict | None = None) -> list[str]:
    """Порядок запасных провайдеров для ai_providers.fallback."""
    return [p for p, _ in rank_working(health or read_health())]
