"""Общие помощники для запускаемых модулей STR.RAG.

Каждый модуль можно запускать напрямую:  python modules/moduleN_*.py --project "Имя"
или как пакет:                            python -m modules.moduleN_*
"""
from __future__ import annotations

import sys
from pathlib import Path

# чтобы `python modules/moduleX.py` находил пакет pmoos
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def banner(title: str) -> None:
    line = "═" * max(8, len(title) + 4)
    print(f"\n{line}\n  {title}\n{line}")


def kv(label: str, value) -> None:
    print(f"  {label:22s}: {value}")


def section_table(rows: list[dict]) -> None:
    """Печать карты разделов ПД (для М1)."""
    print(f"\n  {'':2s} {'Код':8s} {'№':5s} {'Раздел':46s} {'Файлов':6s}")
    print("  " + "─" * 70)
    for r in rows:
        mark = "✓" if r.get("present") else "·"
        extra = " (доп.)" if r.get("extra") else ""
        name = (r.get("name", "")[:44] + extra)
        print(f"  [{mark}] {r.get('code',''):8s} {str(r.get('num','')):5s} {name:46s} {r.get('n_files',0):>4d}")
