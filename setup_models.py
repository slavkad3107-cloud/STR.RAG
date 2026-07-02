"""Скачивание и САМОПРОВЕРКА локальных моделей СтройПроект.

Запускается установщиком (install.bat / install.sh, шаг [5/5]) и вручную:
    python setup_models.py

Делает две вещи:
  1) скачивает в кэш HuggingFace все локальные модели — эмбеддер BAAI/bge-m3 и
     reranker BAAI/bge-reranker-v2-m3 (имена берутся из config, докачка
     возобновляется при обрыве сети);
  2) проверяет, что модели реально РАБОТАЮТ: загружает их ТЕМ ЖЕ кодом, что и
     приложение (включая автопереход safetensors → .bin для bge-m3), считает
     эмбеддинг тестовой фразы и прогоняет реранкер на двух кандидатах.

Коды возврата: 0 — скачано и проверено; 1 — ошибка (в тексте — что делать).
Флаги: --skip-download / --skip-verify / --models "имя1,имя2".
"""
from __future__ import annotations

import argparse
import sys
import traceback

DEFAULT_MODELS = ["BAAI/bge-m3", "BAAI/bge-reranker-v2-m3"]


def _models_from_config() -> list[str]:
    try:
        from pmoos.config import load_config
        c = load_config()
        emb = str(c.get("embedding.model", c.get("embeddings.model", DEFAULT_MODELS[0])))
        rer = str(c.get("reranker.model", DEFAULT_MODELS[1]))
        return [emb, rer]
    except Exception:  # noqa: BLE001
        return list(DEFAULT_MODELS)


def download_all(models: list[str]) -> None:
    """Скачивает все модели в кэш HF (повторный запуск докачивает недостающее)."""
    from huggingface_hub import snapshot_download
    for i, m in enumerate(models, 1):
        print(f"[models] ({i}/{len(models)}) скачивание {m} ...", flush=True)
        snapshot_download(m)
        print(f"[models] ({i}/{len(models)}) {m}: готово", flush=True)


def verify_models() -> None:
    """Функциональная проверка тем же кодом, что использует приложение."""
    import numpy as np
    from pmoos.config import load_config
    cfg = load_config()

    print("[check] эмбеддер: загрузка модели и тестовое кодирование ...", flush=True)
    from pmoos.index.embeddings import Embedder
    emb = Embedder(cfg)
    vec = emb.embed(
        ["Расчёт выбросов загрязняющих веществ в период строительства."],
        use_cache=False,
    )
    assert vec.shape[0] == 1 and vec.shape[1] == emb.dim and emb.dim >= 256, \
        f"неожиданная размерность эмбеддинга: {vec.shape} (dim={emb.dim})"
    assert np.isfinite(vec).all(), "эмбеддинг содержит NaN/Inf"
    print(f"[check] эмбеддер OK: модель={emb.model_name}, dim={emb.dim}, "
          f"устройство={emb.device}", flush=True)

    # освобождаем память (важно для 8 ГБ VRAM) перед загрузкой reranker.
    # ВАЖНО: с v0.17 модели кэшируются в процессном реестре _MODELS (синглтон),
    # поэтому обнуления emb._model недостаточно — нужно убрать сильную ссылку и
    # из реестра, иначе gc/empty_cache не освободят VRAM.
    try:
        from pmoos.index.embeddings import _MODELS as _EMB_MODELS
        _EMB_MODELS.pop(emb._model_key(), None)
        emb._model = None
        import gc
        gc.collect()
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass

    print("[check] reranker: загрузка модели и тестовое ранжирование ...", flush=True)
    from pmoos.retrieval.reranker import Reranker
    rr = Reranker(cfg)
    rr._load()  # ЯВНАЯ загрузка: rerank() глотает ошибки, а проверке нужна честность
    cands = [
        {"id": "a", "text": "Перечень строительной техники приведён в ПОС, таблица 4.1."},
        {"id": "b", "text": "Сети хозяйственно-бытовой канализации, колодцы."},
    ]
    out = rr.rerank("строительная техника по проекту организации строительства",
                    cands, top=2)
    assert out and all("rerank_score" in c for c in out), "reranker не вернул оценок"
    print(f"[check] reranker OK: модель={rr.model_name}; "
          f"лучший кандидат: «{out[0]['text'][:42]}…»", flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Скачивание и самопроверка моделей СтройПроект")
    ap.add_argument("--models", help="список моделей через запятую (по умолчанию — из config)")
    ap.add_argument("--skip-download", action="store_true", help="только проверка")
    ap.add_argument("--skip-verify", action="store_true", help="только скачивание")
    a = ap.parse_args(argv)
    models = ([m.strip() for m in a.models.split(",") if m.strip()]
              if a.models else _models_from_config())
    print(f"[models] модели: {', '.join(models)}", flush=True)
    try:
        if not a.skip_download:
            download_all(models)
        if not a.skip_verify:
            verify_models()
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        print(f"\n[models] ОШИБКА: {e}\n"
              f"Что делать: 1) проверьте интернет и повторите запуск — докачка "
              f"продолжится с места обрыва; 2) если не хватает пакетов — запустите "
              f"install.bat ещё раз; 3) скачать можно и из приложения: Модуль 2 → "
              f"кнопка «Скачать все модели сейчас» (ход — в журнале).", flush=True)
        return 1
    print("\n[models] Все модели скачаны и проверены ✅", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
