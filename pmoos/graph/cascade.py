"""Каскадный анализ изменений по графу зависимостей.

Самый рискованный для экспертизы момент (по ревью-ИИ): при правке ответа на
замечание нужно понимать — какие расчёты затронуты и какие ещё разделы станут
противоречивыми. Здесь это считается обходом графа зависимостей вниз по потоку.
"""
from __future__ import annotations

import threading
from typing import Any

from .dependency import load_graph, build_graph, _node_label

# Кэш загруженного графа по mtime graph.json: downstream/explain_cascade
# вызываются по КАЖДОМУ замечанию (≈150× за прогон М4 на 75 замечаниях), а
# load_graph читал JSON с диска и заново строил networkx каждый раз.
_GRAPH_LOCK = threading.Lock()
_GRAPH_CACHE: dict[str, tuple] = {}


def _graph_sig(project: str):
    from ..paths import project_paths
    try:
        return project_paths(project)["graph"].stat().st_mtime_ns
    except OSError:
        return None


def _cached_graph(project: str):
    sig = _graph_sig(project)
    if sig is None:
        # graph.json отсутствует → load_graph строит граф на лету из inventory.
        # НЕ кэшируем: иначе ключ sig=None «залипает» (None==None) и переиндексация
        # inventory не инвалидирует кэш. Построение на лету дёшево и редко.
        return load_graph(project)
    with _GRAPH_LOCK:
        ent = _GRAPH_CACHE.get(project)
        if ent is not None and ent[0] == sig:
            return ent[1]
    g = load_graph(project)
    with _GRAPH_LOCK:
        _GRAPH_CACHE[project] = (sig, g)
    return g


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
    g = _cached_graph(project)
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


def explain_cascade(project: str, changed_codes: list[str],
                    res: dict | None = None) -> str:
    """Текстовое резюме каскада для показа пользователю/добавления в ответ.

    res — уже посчитанный downstream() (block1 считает его строкой выше;
    без параметра обход графа выполнялся дважды на каждое замечание)."""
    if res is None:
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
