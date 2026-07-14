"""Эмбеддинги (BAAI/bge-m3) с исправлением сразу нескольких проблем из логов:

  1. «загрузка BAAI/bge-m3 на cpu» вместо GPU — теперь device определяется
     автоматически (cuda при наличии), и это видно в логе.
  2. Падение ValueError: torch.load … CVE-2025-32434 — модель грузится из
     safetensors (use_safetensors=True), что снимает запрет на torch.load.
  3. Предупреждение HF про неавторизованные запросы — токен берётся из env,
     если задан; иначе работаем анонимно без падения.
  4. «нет кэша эмбеддингов» — добавлен sqlite-кэш sha256(model+text):
     повторная индексация почти бесплатна, что критично для пауза/возобновление.
  5. OOM на 8 ГБ VRAM — при нехватке памяти автоматически уменьшаем батч и
     чистим кэш CUDA.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading

import numpy as np

from ..config import Config
from ..paths import emb_cache_path

_LOCK = threading.Lock()

# Синглтон загруженных моделей НА ПРОЦЕСС (оптимизация): не перегружать ~2 ГБ в
# VRAM при каждом запуске модуля / каждом ререндере Streamlit. Ключ учитывает
# имя модели, устройство, fp16 и max_length — при смене настроек грузится нужная
# модель, а не отдаётся старая. Streamlit ререндерит скрипт, но НЕ перезапускает
# процесс, поэтому модульный кэш переживает ререндеры.
_MODEL_LOCK = threading.Lock()
_MODELS: dict[tuple, object] = {}


def pick_device(requested: str = "auto") -> str:
    if requested and requested != "auto":
        return requested
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


class _EmbCache:
    """Дисковый кэш векторов: ключ = sha256(model|text).

    WAL-режим (по замечанию ревью) — устойчивее к параллельным воркерам и
    'database is locked'. Колонка last_used позволяет чистить старые записи,
    чтобы кэш не рос бесконечно.
    """

    def __init__(self):
        # dim здесь НЕ нужен (векторы читаются frombuffer как есть) — и его
        # отсутствие позволяет НЕ грузить модель ~2.3 ГБ при 100% попадании в кэш.
        self.con = sqlite3.connect(str(emb_cache_path()), check_same_thread=False)
        try:
            self.con.execute("PRAGMA journal_mode=WAL")
            self.con.execute("PRAGMA synchronous=NORMAL")
            self.con.execute("PRAGMA busy_timeout=5000")
        except Exception:  # noqa: BLE001
            pass
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS emb (k TEXT PRIMARY KEY, v BLOB, last_used REAL)")
        # миграция старой схемы без last_used
        cols = [r[1] for r in self.con.execute("PRAGMA table_info(emb)").fetchall()]
        if "last_used" not in cols:
            try:
                self.con.execute("ALTER TABLE emb ADD COLUMN last_used REAL")
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def key(model: str, text: str) -> str:
        return hashlib.sha256(f"{model}\u0000{text}".encode("utf-8")).hexdigest()

    def get_many(self, keys: list[str]) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        if not keys:
            return out
        rows: list = []
        with _LOCK:
            # батчами по 900: у SQLite лимит переменных (999 в старых сборках) —
            # иначе на больших проектах (тысячи чанков за раз) запрос молча падал бы
            for i in range(0, len(keys), 900):
                part = keys[i:i + 900]
                qmarks = ",".join("?" * len(part))
                rows.extend(self.con.execute(
                    f"SELECT k, v FROM emb WHERE k IN ({qmarks})", part
                ).fetchall())
            if rows:
                import time as _t
                now = _t.time()
                self.con.executemany("UPDATE emb SET last_used=? WHERE k=?",
                                     [(now, k) for k, _ in rows])
                self.con.commit()
        for k, v in rows:
            out[k] = np.frombuffer(v, dtype=np.float32)
        return out

    def put_many(self, items: dict[str, np.ndarray]) -> None:
        if not items:
            return
        import time as _t
        now = _t.time()
        with _LOCK:
            self.con.executemany(
                "INSERT OR REPLACE INTO emb (k, v, last_used) VALUES (?, ?, ?)",
                [(k, v.astype(np.float32).tobytes(), now) for k, v in items.items()],
            )
            self.con.commit()

    def cleanup(self, *, max_rows: int = 2_000_000) -> int:
        """Удалить самые старые записи, если кэш превысил max_rows. Возвращает
        число удалённых. Вызывать по желанию (обслуживание)."""
        with _LOCK:
            (n,) = self.con.execute("SELECT COUNT(*) FROM emb").fetchone()
            if n <= max_rows:
                return 0
            to_del = n - max_rows
            self.con.execute(
                "DELETE FROM emb WHERE k IN (SELECT k FROM emb ORDER BY last_used ASC LIMIT ?)",
                (to_del,))
            self.con.commit()
            return to_del


class Embedder:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.model_name = cfg.get("embedding.model", "BAAI/bge-m3")
        self.device = pick_device(cfg.get("embedding.device", "auto"))
        self.batch_size = int(cfg.get("embedding.batch_size", 16))
        self.max_length = int(cfg.get("embedding.max_length", 1024))
        self.use_safetensors = bool(cfg.get("embedding.use_safetensors", True))
        # fp16 включаем только на cuda (на cpu half() медленнее и местами не
        # поддерживается). ~2× скорость кодирования и вдвое меньше VRAM.
        # По умолчанию ВЫКЛ (opt-in): half-точность слегка меняет векторы, а
        # приоритет пользователя — стабильность выдачи. Включается осознанно;
        # при включении ключ кэша эмбеддингов учитывает fp16 (см. _cache_model_id),
        # и рекомендуется переиндексация для консистентности с базой Qdrant.
        self.fp16 = bool(cfg.get("embedding.fp16", False)) and self.device == "cuda"
        self._model = None
        self._dim: int | None = None
        self._cache: _EmbCache | None = None

    def _model_key(self) -> tuple:
        return (self.model_name, self.device, self.fp16, self.max_length)

    def _load(self):
        if self._model is not None:
            return self._model
        key = self._model_key()
        with _MODEL_LOCK:
            cached = _MODELS.get(key)
            if cached is not None:
                # В реестре храним (модель, фактический fp16). Восстанавливаем
                # фактическую точность: если у первого экземпляра .half() упал
                # (модель осталась fp32), повторный экземпляр НЕ должен считать
                # себя fp16 — иначе _cache_model_id() писал бы fp32-векторы под
                # fp16-ключ кэша. (Срабатывает лишь при opt-in fp16 + сбое half.)
                self._model, self.fp16 = cached
                return self._model
        from sentence_transformers import SentenceTransformer
        print(f"[embeddings] загрузка {self.model_name} на {self.device}"
              f"{' (fp16)' if self.fp16 else ''}", flush=True)
        token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
        # Кэш моделей НЕ задаём параметром: единый каталог определяется HF_HOME
        # (config.py: <данные>/models/hub) — тот же, куда качают setup_models.py
        # и предзагрузка Модуля 2. Так модель не скачивается дважды.
        kwargs = dict(device=self.device)

        def _try(force_safetensors: bool):
            mk = {"use_safetensors": True} if force_safetensors else {}
            try:
                return SentenceTransformer(self.model_name, model_kwargs=mk,
                                           token=token, **kwargs)
            except TypeError:
                # старые версии sentence-transformers без model_kwargs/token
                return SentenceTransformer(self.model_name, **kwargs)

        # Сначала строгий safetensors (фикс CVE-2025-32434). Но некоторые модели
        # (в т.ч. BAAI/bge-m3) публикуются ТОЛЬКО с pytorch_model.bin — тогда
        # откатываемся на .bin: под torch>=2.6 это безопасно (weights_only=True
        # по умолчанию), а torch==2.6 ставится установщиком явно.
        if self.use_safetensors:
            try:
                self._model = _try(True)
            except OSError as e:
                if "model.safetensors" in str(e):
                    print(f"[embeddings] у {self.model_name} нет model.safetensors — "
                          f"загружаю pytorch_model.bin (безопасно: torch>=2.6, weights_only)",
                          flush=True)
                    self._model = _try(False)
                else:
                    raise
        else:
            self._model = _try(False)
        try:
            self._model.max_seq_length = self.max_length
        except Exception:
            pass
        # FP16: половинная точность для инференса на GPU (bge-m3 устойчив к этому).
        if self.fp16:
            try:
                self._model.half()
            except Exception as e:  # noqa: BLE001
                print(f"[embeddings] fp16 не применён ({e}) — остаюсь в fp32", flush=True)
                self.fp16 = False
        # ВАЖНО: кладём модель под ТЕМ ЖЕ ключом, по которому искали (key выше).
        # Если .half() упал и self.fp16 стал False, пересчёт self._model_key()
        # дал бы другой ключ — будущие Embedder (с fp16=True) не нашли бы модель
        # в реестре и грузили бы её заново. Фиксируем ключ один раз.
        # Значение — кортеж (модель, фактический fp16), чтобы повторные экземпляры
        # узнали реальную точность (см. восстановление при cache-hit выше).
        with _MODEL_LOCK:
            _MODELS[key] = (self._model, self.fp16)
        return self._model

    @property
    def dim(self) -> int:
        if self._dim is None:
            m = self._load()
            # метод переименован в новых версиях — поддержим оба
            if hasattr(m, "get_embedding_dimension"):
                self._dim = int(m.get_embedding_dimension())
            else:
                self._dim = int(m.get_sentence_embedding_dimension())
        return self._dim

    def _cache_obj(self) -> _EmbCache:
        if self._cache is None:
            self._cache = _EmbCache()  # без self.dim: не триггерим загрузку модели
        return self._cache

    def _cache_model_id(self) -> str:
        """Идентификатор модели для ключа дискового кэша эмбеддингов.

        Включает fp16, чтобы векторы half- и full-точности НЕ сталкивались в
        кэше (иначе один и тот же текст мог вернуть fp32-вектор из кэша или
        fp16 при промахе — рассинхрон точности запрос/документ)."""
        return f"{self.model_name}|fp16" if self.fp16 else self.model_name

    def _encode(self, texts: list[str]) -> np.ndarray:
        model = self._load()
        bs = self.batch_size
        while True:
            try:
                vecs = model.encode(
                    texts, batch_size=bs, normalize_embeddings=True,
                    convert_to_numpy=True, show_progress_bar=False,
                )
                return vecs.astype(np.float32)
            except RuntimeError as e:
                # CUDA: «out of memory»; CPU (DefaultCPUAllocator): «not enough memory»
                _msg = str(e).lower()
                _oom = any(t in _msg for t in ("out of memory", "not enough memory",
                                               "can't allocate memory"))
                if _oom and bs > 1:
                    bs = max(1, bs // 2)
                    try:
                        import torch
                        torch.cuda.empty_cache()
                    except Exception:
                        pass
                    print(f"[embeddings] OOM — уменьшаю батч до {bs}", flush=True)
                    continue
                raise

    def embed(self, texts: list[str], *, use_cache: bool = True) -> np.ndarray:
        """Векторизует тексты с использованием дискового кэша."""
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        if not use_cache:
            return self._encode(texts)
        cache = self._cache_obj()
        mid = self._cache_model_id()
        keys = [cache.key(mid, t) for t in texts]
        have = cache.get_many(list(dict.fromkeys(keys)))
        # кодируем только УНИКАЛЬНЫЕ промахи: одинаковые тексты в одном вызове
        # (шаблонные строки/повторяющиеся чанки) не гоняются через модель дважды
        miss: dict[str, str] = {}
        for i, k in enumerate(keys):
            if k not in have and k not in miss:
                miss[k] = texts[i]
        if miss:
            new_vecs = self._encode(list(miss.values()))
            new_items = {k: new_vecs[j] for j, k in enumerate(miss)}
            cache.put_many(new_items)
            have.update(new_items)
        return np.vstack([have[k] for k in keys]).astype(np.float32)

    def embed_documents(self, texts: list[str], *, use_cache: bool = True) -> np.ndarray:
        return self.embed(texts, use_cache=use_cache)

    def embed_queries(self, texts: list[str]) -> np.ndarray:
        # для bge-m3 префиксы не обязательны; кэшируем запросы тоже
        return self.embed(texts, use_cache=True)
