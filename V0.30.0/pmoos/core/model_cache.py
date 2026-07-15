"""Проверка локально скачанных моделей HuggingFace.

Замечание пользователя: «проверять скачанные модели прежде чем скачивать».
HF кэшируется в каталоге данных (HF_HOME задан в config.py). Здесь определяем,
есть ли модель уже в кэше, чтобы не качать повторно и показать это в UI.
"""
from __future__ import annotations

from pathlib import Path

from ..paths import models_dir


def _cache_repo_dir(model_id: str) -> Path:
    # формат кэша HF: <HF_HOME>/hub/models--ORG--NAME
    safe = "models--" + model_id.replace("/", "--")
    return models_dir() / "hub" / safe


def is_model_cached(model_id: str) -> bool:
    """True, если в локальном кэше есть снапшот модели с весами (safetensors/bin)."""
    repo = _cache_repo_dir(model_id)
    snapshots = repo / "snapshots"
    if not snapshots.exists():
        return False
    for snap in snapshots.iterdir():
        if not snap.is_dir():
            continue
        weights = list(snap.glob("*.safetensors")) + list(snap.glob("*.bin"))
        cfg = list(snap.glob("config.json"))
        if weights and cfg:
            return True
    return False


def model_status(model_id: str) -> dict:
    return {
        "model": model_id,
        "cached": is_model_cached(model_id),
        "path": str(_cache_repo_dir(model_id)),
    }
