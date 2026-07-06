"""Реранкер кандидатов на базе BAAI/bge-reranker-v2-m3 (CrossEncoder).

Гибридный поиск (dense+BM25) даёт пул кандидатов, а кросс-энкодер точно
переупорядочивает их по релевантности к замечанию. Это заметно повышает
качество ответов — главный приоритет пользователя.

CVE-2025-32434: грузим только safetensors (model_kwargs use_safetensors=True),
device берём из конфигурации (auto -> cuda на 3070ti).
"""
from __future__ import annotations

import threading
from typing import Any

from ..config import Config
from ..core.device import resolve_device

# Синглтон загруженных кросс-энкодеров НА ПРОЦЕСС (см. пояснение в embeddings.py):
# не перегружать реранкер в VRAM при каждом запуске Модуля 4.
_MODEL_LOCK = threading.Lock()
_MODELS: dict[tuple, object] = {}


class Reranker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.model_name = cfg.get("reranker.model", "BAAI/bge-reranker-v2-m3")
        self.enabled = bool(cfg.get("reranker.enabled", True))
        self.device = resolve_device(cfg.get("embedding.device", "auto"))
        self.fp16 = bool(cfg.get("reranker.fp16", True)) and self.device == "cuda"
        self._model = None

    def _model_key(self) -> tuple:
        return (self.model_name, self.device, self.fp16)

    def _load(self):
        if self._model is not None:
            return self._model
        key = self._model_key()
        with _MODEL_LOCK:
            cached = _MODELS.get(key)
            if cached is not None:
                self._model = cached
                return self._model
        import os
        from sentence_transformers import CrossEncoder

        device = self.device
        token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
        # ВАЖНО: НЕ передаём cache_folder/cache_dir — у CrossEncoder в
        # sentence-transformers 3.3 такого параметра нет (был краш TypeError).
        # Кэш моделей единый и задаётся переменной HF_HOME (см. config.py):
        # <данные>/models/hub — туда же качают setup_models.py и предзагрузка М2.
        base: dict[str, Any] = {
            "device": device,
            # окно кросс-энкодера из конфига (дефолт 1024): раньше 512 — реранкер
            # молча усекал хвост чанка и не видел цифру ПДВ/ссылку во второй половине.
            "max_length": int(self.cfg.get("reranker.max_length", 1024)),
        }
        if token:
            base["trust_remote_code"] = False

        def _try(force_safetensors: bool):
            kw = dict(base)
            if force_safetensors:
                # форсируем safetensors (фикс CVE-2025-32434)
                kw["automodel_args"] = {"use_safetensors": True}
            # Сигнатура CrossEncoder различается между версиями
            # sentence-transformers: при TypeError снимаем необязательные
            # аргументы по одному (лесенка), а не один фиксированный.
            while True:
                try:
                    return CrossEncoder(self.model_name, **kw)
                except TypeError:
                    for opt in ("automodel_args", "trust_remote_code", "max_length"):
                        if opt in kw:
                            kw.pop(opt)
                            break
                    else:
                        raise

        try:
            self._model = _try(True)
        except OSError as e:
            # Репозиторий модели без safetensors (только .bin) — безопасный откат
            # (torch>=2.6: weights_only=True по умолчанию).
            if "model.safetensors" in str(e):
                print(f"[reranker] у {self.model_name} нет model.safetensors — "
                      f"загружаю .bin (безопасно: torch>=2.6, weights_only)", flush=True)
                self._model = _try(False)
            else:
                raise
        # FP16: половинная точность кросс-энкодера на GPU (вдвое меньше VRAM,
        # быстрее предсказание). Внутренняя transformers-модель — в .model.
        if self.fp16:
            try:
                self._model.model.half()
            except Exception as e:  # noqa: BLE001
                print(f"[reranker] fp16 не применён ({e}) — остаюсь в fp32", flush=True)
                self.fp16 = False
        # Кладём под ТЕМ ЖЕ ключом, по которому искали (key выше), иначе при сбое
        # .half() запись ушла бы под другой ключ и синглтон не сработал бы.
        with _MODEL_LOCK:
            _MODELS[key] = self._model
        return self._model

    def rerank(self, query: str, candidates: list[dict], *, top: int = 8,
               text_key: str = "text") -> list[dict]:
        """Переупорядочивает кандидатов. Если реранкер отключён или недоступен —
        возвращает исходный список (обрезанный до top)."""
        if not candidates:
            return []
        if not self.enabled:
            return candidates[:top]
        try:
            model = self._load()
        except Exception:
            return candidates[:top]
        pairs = [(query, c.get(text_key, "")) for c in candidates]
        scores = self._predict(model, pairs)
        for c, s in zip(candidates, scores):
            c["rerank_score"] = float(s)
        ranked = sorted(candidates, key=lambda x: x.get("rerank_score", 0.0), reverse=True)
        return ranked[:top]

    def _predict(self, model, pairs: list[tuple]):
        """predict с адаптивным батчем при нехватке VRAM (как у эмбеддера).

        Реранк детерминирован по паре (логиты пары не зависят от соседей в батче),
        поэтому дробление батча НЕ меняет порядок/качество — растёт только
        стабильность на 8-ГБ GPU (единственная тяжёлая стадия М4 без OOM-защиты)."""
        bs = int(self.cfg.get("embedding.batch_size", 16))
        while True:
            try:
                return model.predict(pairs, batch_size=bs)
            except RuntimeError as e:
                if "out of memory" in str(e).lower() and bs > 1:
                    bs = max(1, bs // 2)
                    try:
                        import torch
                        torch.cuda.empty_cache()
                    except Exception:  # noqa: BLE001
                        pass
                    print(f"[reranker] OOM — уменьшаю батч до {bs}", flush=True)
                    continue
                raise
