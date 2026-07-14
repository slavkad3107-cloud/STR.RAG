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


_UI_PROBE: dict = {}


def probe_device_ui(requested: str = "auto") -> str:
    """Дешёвая проба устройства ТОЛЬКО для подписей в интерфейсе.

    resolve_device импортирует torch (секунды + сотни МБ RAM при первом рендере
    хаба — только ради подписи «GPU/CPU»). Здесь: если torch уже импортирован —
    спрашиваем его (истина); иначе проверяем наличие драйвера NVIDIA (nvcuda.dll /
    libcuda) без импорта torch. Вычислительный путь (эмбеддер/реранкер) по-прежнему
    ходит через resolve_device."""
    if requested and requested != "auto":
        return requested
    if "v" in _UI_PROBE:
        return _UI_PROBE["v"]
    import sys
    if "torch" in sys.modules:  # уже загружен реальной работой — точный ответ
        _UI_PROBE["v"] = resolve_device("auto")
        return _UI_PROBE["v"]
    dev = "cpu"
    try:
        import ctypes
        if sys.platform == "win32":
            ctypes.WinDLL("nvcuda.dll")
            dev = "cuda"
        else:
            ctypes.CDLL("libcuda.so.1")
            dev = "cuda"
    except Exception:  # noqa: BLE001 — нет драйвера NVIDIA → CPU
        dev = "cpu"
    _UI_PROBE["v"] = dev
    return dev
