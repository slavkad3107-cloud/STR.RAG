"""Конфигурация системы + ключи ИИ + маршрутизация моделей.

Здесь исправлены два замечания пользователя:
  1. «опять не видит ключ апи» / «перепроверь, в старом приложении не брался
      ключ из .env» — теперь .env ГАРАНТИРОВАННО загружается (load_dotenv),
      а ключ берётся в порядке: явный аргумент -> config.yaml(если непустой)
      -> переменная окружения (.env). Пустые строки трактуются как «нет ключа».
  2. «при выборе провайдера должна меняться автоматически модель в разных
      модулях» — функция model_for(provider, role) и resolve(module) дают
      готовую пару (провайдер, модель) для каждого модуля.
"""
from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from .paths import APP_ROOT, data_root, models_dir

# --- загрузка .env (ключевой фикс) -----------------------------------------
try:
    from dotenv import load_dotenv, set_key, find_dotenv
except Exception:  # pragma: no cover
    load_dotenv = set_key = find_dotenv = None  # type: ignore


def _load_env() -> None:
    """Грузим .env из нескольких мест. data_dir/.env имеет приоритет над
    app/.env (override=True), чтобы пользовательские ключи всегда побеждали."""
    if load_dotenv is None:
        return
    for p in (APP_ROOT / ".env", Path.cwd() / ".env"):
        if p.exists():
            load_dotenv(p, override=False)
    user_env = data_root() / ".env"
    if user_env.exists():
        load_dotenv(user_env, override=True)


_load_env()

# Кэш моделей HuggingFace кладём в каталог данных, а не в профиль пользователя,
# чтобы «проверять скачанные модели прежде чем скачивать» (замечание пользователя)
# и чтобы кэш переживал переустановку приложения.
os.environ.setdefault("HF_HOME", str(models_dir()))
os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", str(models_dir()))
# Снимаем шумное предупреждение HF про неавторизованные запросы, если токена нет.
if os.environ.get("HF_TOKEN"):
    os.environ.setdefault("HUGGINGFACE_HUB_TOKEN", os.environ["HF_TOKEN"])

# Соответствие провайдер -> имя переменной окружения с ключом.
ENV_KEYS: dict[str, tuple[str, ...]] = {
    "deepseek": ("DEEPSEEK_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "kimi": ("MOONSHOT_API_KEY", "KIMI_API_KEY"),
    "mistral": ("MISTRAL_API_KEY",),
    "ollama": (),  # локально, ключ не нужен
}

# ВАЖНО: имена моделей — это значения по умолчанию, их можно (нужно) править
# в config.yaml под актуальные на момент использования. Роли:
#   answer/review — максимальное качество (главное требование пользователя),
#   extract/expand — дешёвые/быстрые операции (парсинг, классификация, перефраз).
DEFAULT_AI: dict[str, Any] = {
    "default_provider": "deepseek",
    "providers": {
        "deepseek": {
            "base_url": "https://api.deepseek.com",
            "answer": "deepseek-reasoner",
            "review": "deepseek-reasoner",
            "extract": "deepseek-chat",
            "expand": "deepseek-chat",
            "supports_json_mode": True,
        },
        "openai": {
            "base_url": "https://api.openai.com/v1",
            "answer": "gpt-4o",
            "review": "gpt-4o",
            "extract": "gpt-4o-mini",
            "expand": "gpt-4o-mini",
            "supports_json_mode": True,
        },
        "gemini": {
            "base_url": "",  # SDK google-generativeai
            "answer": "gemini-1.5-pro",
            "review": "gemini-1.5-pro",
            "extract": "gemini-1.5-flash",
            "expand": "gemini-1.5-flash",
            "supports_json_mode": True,
        },
        "anthropic": {
            "base_url": "https://api.anthropic.com",
            "answer": "claude-3-5-sonnet-latest",
            "review": "claude-3-5-sonnet-latest",
            "extract": "claude-3-5-haiku-latest",
            "expand": "claude-3-5-haiku-latest",
            "supports_json_mode": False,
        },
        "kimi": {
            # Kimi / Moonshot AI — OpenAI-совместимый API.
            # Для РФ/СНГ может подойти .ai; в Китае — https://api.moonshot.cn/v1
            "base_url": "https://api.moonshot.ai/v1",
            "answer": "moonshot-v1-32k",
            "review": "moonshot-v1-32k",
            "extract": "moonshot-v1-8k",
            "expand": "moonshot-v1-8k",
            "supports_json_mode": True,
        },
        "mistral": {
            "base_url": "https://api.mistral.ai/v1",
            "answer": "mistral-large-latest",
            "review": "mistral-large-latest",
            "extract": "mistral-small-latest",
            "expand": "mistral-small-latest",
            "supports_json_mode": True,
        },
        "ollama": {
            "base_url": "http://localhost:11434",
            # по умолчанию компактные 7–8B модели (идут и на CPU/скромной GPU);
            # список реальных моделей подтягивается из `ollama list` (core/ollama_utils.py)
            "answer": "qwen2.5:7b-instruct",
            "review": "qwen2.5:7b-instruct",
            "extract": "qwen2.5:7b-instruct",
            "expand": "qwen2.5:7b-instruct",
            "supports_json_mode": True,
        },
    },
    # По умолчанию все модули используют default_provider. Можно переопределить:
    #   modules: { module4: { provider: gemini } }
    "modules": {},
    "concurrency": 4,          # параллельные запросы к API (asyncio)
    "temperature": 0.1,
    "max_tokens": 4096,
    "use_cache": True,         # кэш ответов ИИ на диск
    "json_repair_retry": True, # один повтор «верни ТОЛЬКО JSON» при сбое парсинга
    # Резервный провайдер при сбое основного (сеть/лимиты/API): например "ollama"
    # (локально, без ключа) или "openai". Пусто = выключено.
    "fallback_provider": "",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "object_type": "площадной",  # площадной | линейный — влияет на состав ПД (ПП-87)
    "ai": DEFAULT_AI,
    "embedding": {
        "model": "BAAI/bge-m3",
        "device": "auto",        # auto -> cuda при наличии, иначе cpu
        "batch_size": 16,
        "use_safetensors": True, # фикс CVE-2025-32434 (torch.load)
        "max_length": 1024,
        # half-точность эмбеддера на GPU: ~2× скорость, вдвое меньше VRAM.
        # OPT-IN (по умолчанию False): слегка меняет векторы; при включении
        # рекомендуется переиндексация (ключ кэша учитывает fp16). Приоритет —
        # стабильность выдачи.
        "fp16": False,
    },
    "reranker": {
        "enabled": True,
        "model": "BAAI/bge-reranker-v2-m3",
        # half-точность кросс-энкодера на GPU: безопасно (логиты считаются заново
        # каждый раз, нет рассинхрона с индексом), даёт выигрыш VRAM/скорости.
        "fp16": True,
        # окно кросс-энкодера в токенах. Было захардкожено 512 → реранкер судил
        # только по началу чанка (хвост с цифрой ПДВ/ссылкой на СП молча усекался).
        # bge-reranker-v2-m3 поддерживает до 1024+; выравниваем с embedding.max_length.
        "max_length": 1024,
    },
    "retrieval": {
        "top_k": 8,
        "candidates": 60,  # глубже пул перед реранком (было 40): лучше recall, fp16-реранкер компенсирует стоимость
        "use_bm25": True,
        "use_rerank": True,
        "use_query_expansion": True,
        "expansions": 3,
        "expand_context": True,    # возвращать соседние чанки для полноты контекста
        "context_neighbors": 1,    # сколько соседей с каждой стороны
        "merge_tables": True,      # склеивать подряд идущие табличные чанки в таблицу
        # взвешенный RRF: BM25 (1 список) не должен тонуть среди N dense-списков
        # расширения. normalize_dense=True делит вклад dense на число dense-списков,
        # чтобы вес семантики не рос линейно с числом перефраз (иначе точные коды
        # ГОСТ/СП/СанПиН, которые ловит только BM25, переголашиваются ~4:1).
        "rrf_k": 60,
        "bm25_weight": 1.0,
        "dense_weight": 1.0,
        "rrf_normalize_dense": True,
        # near-dup фильтр в пуле кандидатов (версии одного тома вытесняют разнообразие).
        # opt-in: по умолчанию выкл — включать при смешивании версий в одном проекте.
        "dedup_near": False,
        "dedup_near_threshold": 0.9,
    },
    "chunking": {
        "size": 1200, "overlap": 200, "min_chunk": 80,
        # режим нарезки: "char" (по умолчанию, как было) | "semantic" (по пунктам
        # НПА, ~target_tokens токенов bge-m3). Переключение требует ПЕРЕИНДЕКСАЦИИ
        # (кнопка «Переиндексировать заново» в М2). A/B: scripts/eval_golden.py run до/после.
        "mode": "char",
        "target_tokens": 512,
        "chars_per_token": 3.2,
    },
    "qdrant": {"mode": "embedded", "url": "http://localhost:6333"},
    "memory": {
        "enabled": True,
        "k": 2,
        "semantic": True,   # поиск похожих замечаний эмбеддингами bge-m3 (иначе Jaccard)
        "min_sim": 0.45,    # порог косинусной близости для few-shot примеров
    },
    "ocr": {"enabled": True, "min_text_chars": 200, "lang": "rus+eng",
            # предел страниц на один PDF при OCR: защита от «зависания» на гигантских
            # сканах (сотни страниц). 0 = без лимита. Пропущенные страницы логируются.
            "max_pages": 0},
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    """Лёгкая обёртка над dict с точечным доступом get('ai.providers')."""

    def __init__(self, data: dict[str, Any]):
        self.data = data

    # точечный доступ: cfg.get("retrieval.top_k", 8)
    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return default
        return cur

    def set(self, dotted: str, value: Any) -> None:
        """Установить вложенное значение по точечному пути (создавая словари)."""
        parts = dotted.split(".")
        cur = self.data
        for part in parts[:-1]:
            nxt = cur.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[part] = nxt
            cur = nxt
        cur[parts[-1]] = value

    # --- ИИ: маршрутизация провайдер/модель --------------------------------
    def default_provider(self) -> str:
        return self.get("ai.default_provider", "deepseek")

    def resolve_provider(self, module: str | None = None) -> str:
        if module:
            p = self.get(f"ai.modules.{module}.provider")
            if p:
                return p
        return self.default_provider()

    def model_for(self, provider: str, role: str = "answer") -> str:
        prov = self.get(f"ai.providers.{provider}", {}) or {}
        return prov.get(role) or prov.get("answer") or ""

    def base_url(self, provider: str) -> str:
        return self.get(f"ai.providers.{provider}.base_url", "") or ""

    def supports_json_mode(self, provider: str) -> bool:
        return bool(self.get(f"ai.providers.{provider}.supports_json_mode", False))

    def api_key(self, provider: str) -> str:
        """Ключ ИИ: config.yaml (если непустой) -> .env/окружение. Пустые -> ''."""
        # 1) явно прописанный в config.yaml ключ (необязательно)
        explicit = (self.get(f"ai.providers.{provider}.api_key") or "").strip()
        if explicit:
            return explicit
        # 2) переменные окружения (.env уже загружен)
        for var in ENV_KEYS.get(provider, ()):  # type: ignore[arg-type]
            val = (os.environ.get(var) or "").strip()
            if val:
                return val
        return ""

    def has_key(self, provider: str) -> bool:
        return provider == "ollama" or bool(self.api_key(provider))

    def save(self, path: Path | None = None) -> None:
        path = path or (data_root() / "config.yaml")
        # ключи в config.yaml не сохраняем (они в .env) — чистим перед записью
        clean = deepcopy(self.data)
        for prov in clean.get("ai", {}).get("providers", {}).values():
            prov.pop("api_key", None)
        path.write_text(
            yaml.safe_dump(clean, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )


def config_path() -> Path:
    return data_root() / "config.yaml"


def load_config() -> Config:
    """Грузим config.yaml из каталога данных, мёржим с дефолтами."""
    path = config_path()
    user: dict[str, Any] = {}
    if path.exists():
        try:
            user = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            user = {}
    return Config(_deep_merge(DEFAULT_CONFIG, user))


def write_env_key(provider_or_var: str, value: str) -> Path:
    """Безопасная запись ключа в data_dir/.env.

    Старая ручная реализация теряла префиксы и могла портить файл — поэтому
    используем dotenv.set_key (корректно экранирует). Принимает либо имя
    провайдера (deepseek/openai/...), либо прямое имя переменной.
    """
    var = provider_or_var
    if provider_or_var in ENV_KEYS:
        keys = ENV_KEYS[provider_or_var]
        var = keys[0] if keys else provider_or_var.upper() + "_API_KEY"
    env_path = data_root() / ".env"
    env_path.touch(exist_ok=True)
    if set_key is not None:
        set_key(str(env_path), var, value.strip(), quote_mode="never")
    else:  # запасной путь без python-dotenv
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
        out, found = [], False
        for ln in lines:
            if ln.strip().startswith(f"{var}="):
                out.append(f"{var}={value.strip()}")
                found = True
            else:
                out.append(ln)
        if not found:
            out.append(f"{var}={value.strip()}")
        env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    # сразу подхватываем в текущий процесс
    os.environ[var] = value.strip()
    return env_path
