"""Граф зависимостей между разделами ПД (МОДУЛЬ 3).

Эксперты часто пишут замечание к ПМООС, корень которого — в смежном разделе
(ПОС задаёт технику → расчёт выбросов → рассеивание → СЗЗ). Чтобы ничего не
пропустить, строим направленный граф «что от чего зависит по данным» и умеем
показывать каскад: какие разделы/расчёты затронет изменение.

Граф строится из двух источников:
  * доменная карта зависимостей (DOMAIN_EDGES) — экспертные знания по ООС;
  * фактически загруженные разделы проекта (из inventory) — чтобы подсветить,
    какие связи реально присутствуют, а каких данных не хватает.

Хранение: networkx -> JSON (node-link) в project/graph.json. Для визуализации
в UI отдаём узлы/рёбра, пригодные для pyvis.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..paths import project_paths
from ..ingest.sections import section_name, all_sections

# Доменные зависимости по данным (откуда -> куда «течёт» информация).
# Узлы РАЗДЕЛОВ используют те же латинские коды, что и инвентаризация
# (POS/OOS/TKR/IEI/…), чтобы граф согласовывался с остальной системой.
DOMAIN_EDGES: list[tuple[str, str, str]] = [
    # (источник, потребитель, по каким данным)
    ("PZU", "POS", "генплан, площадки, временные сооружения"),
    ("TKR", "POS", "конструктивные/технологические решения"),
    ("POS", "OOS", "строительная техника, этапы, потребности"),
    ("IEI", "OOS", "фоновое состояние среды, ООПТ, краснокнижные виды"),
    ("IGMI", "OOS", "метео-фон, климат для расчёта рассеивания"),
    ("IGDI", "OOS", "рельеф, гидрология поверхностных вод"),
    ("IGI", "OOS", "геология, грунты, отходы грунта"),
    ("POS", "EMISSIONS", "техника и оборудование -> валовые/максимальные выбросы"),
    ("EMISSIONS", "DISPERSION", "источники выбросов -> расчёт приземных концентраций"),
    ("IGMI", "DISPERSION", "метеопараметры, коэффициент стратификации"),
    ("DISPERSION", "SZZ", "изолинии концентраций -> обоснование СЗЗ"),
    ("POS", "WASTE", "виды работ -> образование отходов строительства"),
    ("IEI", "WASTE", "снятие плодородного слоя, биоотходы"),
    ("OOS", "EMISSIONS", "раздел ООС агрегирует расчёт выбросов"),
    ("OOS", "WASTE", "раздел ООС агрегирует расчёт отходов"),
    ("OOS", "SZZ", "раздел ООС включает обоснование СЗЗ"),
]

# Виртуальные «расчётные» узлы (не разделы ПП-87, а данные внутри ООС).
VIRTUAL_NODES = {
    "EMISSIONS": "Расчёт выбросов ЗВ",
    "DISPERSION": "Расчёт рассеивания (УПРЗА)",
    "SZZ": "Обоснование СЗЗ",
    "WASTE": "Расчёт образования отходов",
}


def _node_label(code: str) -> str:
    if code in VIRTUAL_NODES:
        return VIRTUAL_NODES[code]
    name = section_name(code)  # уже в формате «ПОС — Проект организации строительства»
    return name if name and name != code else code


def build_graph(project: str | None = None, present_sections: set[str] | None = None):
    """Строит networkx.DiGraph. Если задан present_sections — отмечает узлы,
    данные по которым реально загружены (present=True)."""
    import networkx as nx
    g = nx.DiGraph()
    present_sections = present_sections or set()

    def _add(code: str):
        if code not in g:
            g.add_node(code, label=_node_label(code),
                       kind="virtual" if code in VIRTUAL_NODES else "section",
                       present=code in present_sections)

    for src, dst, why in DOMAIN_EDGES:
        _add(src)
        _add(dst)
        g.add_edge(src, dst, data=why)
    return g


def present_sections_from_inventory(project: str) -> set[str]:
    inv_path = project_paths(project)["inventory"]
    if not inv_path.exists():
        return set()
    try:
        inv = json.loads(inv_path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    present = set()
    for item in inv.get("files", []):
        sec = item.get("section")
        if sec and sec != "UNKNOWN":
            present.add(sec)
    return present


def save_graph(project: str, g) -> Path:
    import networkx as nx
    data = nx.node_link_data(g)
    p = project_paths(project)["graph"]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load_graph(project: str):
    import networkx as nx
    p = project_paths(project)["graph"]
    if not p.exists():
        return build_graph(project, present_sections_from_inventory(project))
    data = json.loads(p.read_text(encoding="utf-8"))
    return nx.node_link_graph(data, directed=True)


def to_vis(g) -> dict[str, Any]:
    """Узлы/рёбра для визуализации (pyvis/streamlit-agraph)."""
    nodes = []
    for n, attrs in g.nodes(data=True):
        nodes.append({
            "id": n,
            "label": attrs.get("label", n),
            "group": attrs.get("kind", "section"),
            "present": attrs.get("present", False),
            "color": "#2e7d32" if attrs.get("present") else (
                "#9e9e9e" if attrs.get("kind") == "section" else "#1565c0"),
        })
    edges = [{"from": u, "to": v, "title": d.get("data", ""), "arrows": "to"}
             for u, v, d in g.edges(data=True)]
    return {"nodes": nodes, "edges": edges}


def build_and_save(project: str):
    g = build_graph(project, present_sections_from_inventory(project))
    save_graph(project, g)
    return g


def write_vis_html(project: str, vis: dict | None = None) -> str:
    """Сохранить интерактивный граф (pyvis) в out/. При отсутствии pyvis —
    простой HTML со списком связей (graceful fallback)."""
    if vis is None:
        vis = to_vis(build_graph(project, present_sections_from_inventory(project)))
    out = project_paths(project)["out"]
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"Граф_связей_{project}.html"
    try:
        from pyvis.network import Network
    except Exception:
        rows = "".join(
            f"<li>{e['from']} → {e['to']} <i>{e.get('title','')}</i></li>" for e in vis["edges"]
        )
        path.write_text(
            f"<html><meta charset='utf-8'><body><h2>Связи разделов: {project}</h2>"
            f"<ul>{rows}</ul></body></html>", encoding="utf-8")
        return str(path)
    net = Network(height="600px", width="100%", directed=True, notebook=False)
    net.barnes_hut()
    for n in vis["nodes"]:
        net.add_node(n["id"], label=n["label"], color=n["color"],
                     title=f"{n['label']} ({'присутствует' if n['present'] else 'нет данных'})")
    for e in vis["edges"]:
        net.add_edge(e["from"], e["to"], title=e.get("title", ""), arrows="to")
    net.write_html(str(path), notebook=False, open_browser=False)
    return str(path)
