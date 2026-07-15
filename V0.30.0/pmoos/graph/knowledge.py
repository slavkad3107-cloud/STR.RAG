"""Накопительный граф знаний по ВСЕМ проектам (пункт 1, вариант B).

В отличие от графа Модуля 3 (статические связи разделов одного проекта), здесь
копится «память связей» по всем проектам: какие фактические сущности (техника,
ЗВ) встречались в каких разделах и проектах. Граф хранится на диске
(data_root()/graph/knowledge.json, формат node-link) и растёт по мере работы —
без графового сервера. Когда он реально вырастет, его можно будет перенести в
Neo4j/Kùzu (пункт 1, вариант C), но сейчас NetworkX достаточно.

Источник данных — answers.json и inventory.json проекта (файлы ПД не хранятся):
  • project → HAS → section            (присутствующие разделы);
  • section → USES → equipment/pollutant (сущности, извлечённые из ответов).
Идентификаторы узлов снабжены префиксом типа: proj:/sec:/eq:/pol:.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..paths import data_root
from ..ingest.sections import section_name
from ..entities import find_equipment, find_pollutants


def _kg_path() -> Path:
    d = data_root() / "graph"
    d.mkdir(parents=True, exist_ok=True)
    return d / "knowledge.json"


# кэш на процесс по сигнатуре файла: граф накопительный по ВСЕМ проектам и растёт,
# а stats()/поиск в UI дергали полный json.loads + сборку DiGraph на каждый rerun
_KG_CACHE: dict[str, Any] = {"sig": None, "g": None}


def _kg_sig(p: Path):
    try:
        stt = p.stat()
        return (stt.st_mtime_ns, stt.st_size)
    except OSError:
        return None


def load_knowledge():
    import networkx as nx
    p = _kg_path()
    if not p.exists():
        return nx.DiGraph()
    sig = _kg_sig(p)
    if sig is not None and _KG_CACHE["sig"] == sig and _KG_CACHE["g"] is not None:
        return _KG_CACHE["g"]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        g = nx.node_link_graph(data, directed=True, edges="links")
    except Exception:  # noqa: BLE001
        return nx.DiGraph()
    _KG_CACHE.update(sig=sig, g=g)
    return g


def save_knowledge(g) -> Path:
    import networkx as nx
    p = _kg_path()
    data = nx.node_link_data(g, edges="links")
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _KG_CACHE.update(sig=_kg_sig(p), g=g)  # кэш когерентен записи
    return p


def _ensure_node(g, nid: str, **attrs) -> None:
    if g.has_node(nid):
        g.nodes[nid].update({k: v for k, v in attrs.items() if v is not None})
    else:
        g.add_node(nid, **attrs)


def _bump_edge(g, u: str, v: str, kind: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    if g.has_edge(u, v):
        d = g[u][v]
        d["weight"] = d.get("weight", 1) + 1
        d["last_seen"] = now
        d.setdefault("first_seen", now)
    else:
        g.add_edge(u, v, kind=kind, weight=1, first_seen=now, last_seen=now)


def update_from_project(project: str) -> dict[str, int]:
    """Влить сущности/связи проекта в накопительный граф. Идемпотентно (вес растёт).
    Возвращает статистику {nodes, edges, entities}."""
    from ..pipeline.block1_answers import load_answers
    from ..ingest.inventory import load_inventory

    g = load_knowledge()
    pnode = f"proj:{project}"
    _ensure_node(g, pnode, kind="project", label=project, updated=datetime.now().isoformat(timespec="seconds"))

    # присутствующие разделы (из инвентаря)
    inv = load_inventory(project) or {}
    for code in inv.get("sections_present", []):
        snode = f"sec:{code}"
        _ensure_node(g, snode, kind="section", label=section_name(code), code=code)
        _bump_edge(g, pnode, snode, "HAS")

    # сущности из принятых/предложенных ответов
    data = load_answers(project)
    n_ent = 0
    for a in data.get("answers", []):
        text = " ".join(filter(None, [
            a.get("answer", ""), a.get("correction", ""), a.get("user_answer", ""),
            " ".join(s.get("snippet", "") for s in (a.get("sources") or [])),
        ]))
        # раздел-источник ответа
        sec = (a.get("sources") or [{}])[0].get("section", "")
        snode = f"sec:{sec}" if sec else None
        if snode:
            _ensure_node(g, snode, kind="section", label=section_name(sec), code=sec)
            _bump_edge(g, pnode, snode, "HAS")
        for canon in find_equipment(text):
            en = f"eq:{canon}"
            _ensure_node(g, en, kind="equipment", label=canon)
            _bump_edge(g, pnode, en, "USES")
            if snode:
                _bump_edge(g, snode, en, "USES")
            n_ent += 1
        for pol in find_pollutants(text):
            pn = f"pol:{pol['code']}"
            _ensure_node(g, pn, kind="pollutant", label=pol["name"], code=pol["code"])
            _bump_edge(g, pnode, pn, "EMITS")
            if snode:
                _bump_edge(g, snode, pn, "EMITS")
            n_ent += 1

    save_knowledge(g)
    return {"nodes": g.number_of_nodes(), "edges": g.number_of_edges(), "entities": n_ent}


# ─────────────────────────── запросы по графу знаний ───────────────────────────
def stats() -> dict[str, Any]:
    g = load_knowledge()
    kinds: dict[str, int] = {}
    for _, a in g.nodes(data=True):
        kinds[a.get("kind", "?")] = kinds.get(a.get("kind", "?"), 0) + 1
    return {"nodes": g.number_of_nodes(), "edges": g.number_of_edges(), "by_kind": kinds}


def projects_with_entity(name_fragment: str) -> list[str]:
    """В каких проектах встречалась сущность (техника/ЗВ) по фрагменту названия."""
    g = load_knowledge()
    frag = name_fragment.lower()
    targets = [n for n, a in g.nodes(data=True)
               if a.get("kind") in ("equipment", "pollutant")
               and frag in str(a.get("label", "")).lower()]
    projs: set[str] = set()
    for t in targets:
        for u, _ in g.in_edges(t):
            if g.nodes[u].get("kind") == "project":
                projs.add(g.nodes[u].get("label", u))
    return sorted(projs)


def to_vis(limit: int = 300) -> dict:
    g = load_knowledge()
    color = {"project": "#6a1b9a", "section": "#2e7d32", "equipment": "#1565c0", "pollutant": "#b00020"}
    nodes, edges = [], []
    for i, (n, a) in enumerate(g.nodes(data=True)):
        if i >= limit:
            break
        nodes.append({"id": n, "label": a.get("label", n), "group": a.get("kind", "?"),
                      "color": color.get(a.get("kind"), "#888")})
    keep = {n["id"] for n in nodes}
    for u, v, d in g.edges(data=True):
        if u in keep and v in keep:
            edges.append({"from": u, "to": v, "title": d.get("kind", ""), "arrows": "to"})
    return {"nodes": nodes, "edges": edges}
