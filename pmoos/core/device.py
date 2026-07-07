"""Лёгкий помощник выбора устройства (cuda/cpu) без тяжёлых импортов.

auto -> cuda при наличии видеокарты NVIDIA, иначе cpu (по умолчанию).
"""
from __future__ import annotations


def resolve_device(requested: str = "auto") -> str:
    if requested and requested != "auto":
        return requested
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"
