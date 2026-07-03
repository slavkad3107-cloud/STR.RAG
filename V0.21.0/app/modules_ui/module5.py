"""СтройПроект · Модуль 5 (Корректировка ПМООС) — отдельное Streamlit-приложение.

Запуск:  streamlit run app/modules_ui/module5.py
Переиспользует функции единого хаба (app/hub.py); данные/проекты/индекс — общие.
"""
from __future__ import annotations

import streamlit as st

st.set_page_config(page_title="М5 · Корректировка ПМООС", page_icon="🌍", layout="wide")

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.hub import _cfg, sidebar, apply_font_css, tab_m5


def main() -> None:
    st.session_state["_uid"] = 0
    apply_font_css(_cfg())
    project, object_type = sidebar()
    if not project:
        st.info("Создайте или выберите проект слева, чтобы начать.")
        return
    st.title(f"Проект: {project}")
    tab_m5(project, object_type)


main()
