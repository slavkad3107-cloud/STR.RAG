"""Каскадный анализ изменений по графу зависимостей.

Самый рискованный для экспертизы момент (по ревью-ИИ): при правке ответа на
замечание нужно понимать — какие расчёты затронуты и какие ещё разделы станут
противоречивыми. Здесь это считается обходом графа зависимостей вниз по потоку.
"""
from __future__ import annotations

from typing import Any

from .dependency import load_graph, build_graph, _node_label


def downstream(project: str, changed_codes: list[str], *, max_depth: int = 5) -> dict[str, Any]:
    """Возвращает разделы/расчёты, затронутые изменением changed_codes.

    Результат:
      {
        "changed": [...],
        "affected": [{"code","label","via":[путь], "depth"}],
        "order": [коды в порядке обновления (топологически)],
      }
    """
    import networkx as nx
    g = load_graph(project)
    changed = [c for c in changed_codes if c in g]
    affected: dict[str, dict] = {}

    for start in changed:
        # BFS вниз по рёбрам зависимости
        for tgt in nx.descendants(g, start):
            try:
                path = nx.shortest_path(g, start, tgt)
            except Exception:
                path = [start, tgt]
            depth = len(path) - 1
            prev = affected.get(tgt)
            if prev is None or depth < prev["depth"]:
                affected[tgt] = {
                    "code": tgt, "label": _node_label(tgt),
                    "via": path, "depth": depth,
                }

    # топологический порядок обновления подграфа (changed + affected)
    sub_nodes = set(changed) | set(affected.keys())
    sub = g.subgraph(sub_nodes)
    try:
        order = list(nx.topological_sort(sub))
    except Exception:
        order = list(sub_nodes)

    return {
        "changed": changed,
        "affected": sorted(affected.values(), key=lambda x: (x["depth"], x["code"])),
        "order": order,
    }


def explain_cascade(project: str, changed_codes: list[str]) -> str:
    """Текстовое резюме каскада для показа пользователю/добавления в ответ."""
    res = downstream(project, changed_codes)
    if not res["changed"]:
        return "Изменяемые разделы не найдены в графе зависимостей."
    lines = [f"Изменение в: {', '.join(res['changed'])}"]
    if not res["affected"]:
        lines.append("Прямых зависимых разделов не обнаружено.")
    else:
        lines.append("Затронутые разделы/расчёты (проверить на согласованность):")
        for a in res["affected"]:
            via = " → ".join(a["via"])
            lines.append(f"  • {a['label']} (глубина {a['depth']}; путь: {via})")
    return "\n".join(lines)
