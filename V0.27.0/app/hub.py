"""СтройПроект v0.27.0 «Verified» — единый интерфейс (Streamlit).

Запуск:  streamlit run app/hub.py
Модули также запускаются ОТДЕЛЬНО:
  • как Streamlit-приложения — run_ui_module.bat или
    streamlit run app/modules_ui/moduleN.py (переиспользуют функции этого файла);
  • как CLI — из папки modules/ (run_module.bat).

Здесь учтены требования пользователя:
  #4  — убран «запрос смежникам» (его здесь нет);
  #5/#8 — переключатель площадной/линейный меняет состав ПД; раздел файла —
          это догадка с кандидатами, пользователь подтверждает (не навязываем);
  #7  — карта разделов таблицей + версии + хронология; #9 — файлы не сохраняем;
  #11/#12 — индексация в фоне с прогрессом/паузой/возобновлением;
  #13/#14 — список моделей Ollama и авто-смена модели по провайдеру.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pmoos import __version__, __codename__
from pmoos.config import load_config
from pmoos.paths import project_paths
from pmoos.projects import list_projects, register_project
import app.components as C  # type: ignore

# set_page_config НЕ на уровне модуля: иначе hub.py нельзя импортировать из
# отдельных модульных приложений (app/modules_ui/*) без побочного эффекта.
# Вызывается в main() и в каждом модульном приложении ПЕРВОЙ Streamlit-командой.
PAGE_TITLE = "СтройПроект"
PAGE_ICON = "🌍"


def apply_font_css(cfg) -> None:
    """Размер шрифта из config (ui.font_size). Вызывать ПОСЛЕ set_page_config."""
    fs = int(cfg.get("ui.font_size", 19))
    st.markdown(
        f"""
        <style>
          html, body, [class*="css"], .stMarkdown, .stText, p, li, label,
          .stTabs [data-baseweb="tab"] {{ font-size: {fs}px !important; }}
          .stDataFrame, .stTable {{ font-size: {fs - 1}px !important; }}
          h1 {{ font-size: {fs + 13}px !important; }}
          h2 {{ font-size: {fs + 7}px !important; }}
          h3 {{ font-size: {fs + 3}px !important; }}
          .stButton button, .stDownloadButton button {{ font-size: {fs - 1}px !important; }}
          section[data-testid="stSidebar"] * {{ font-size: {fs - 2}px !important; }}
          div[data-testid="stMetricValue"] {{ font-size: {fs + 5}px !important; }}
          .stSelectbox, .stTextInput input, .stTextArea textarea {{ font-size: {fs - 1}px !important; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


# ─────────────────────────────── состояние ───────────────────────────────
def _cfg():
    if "cfg" not in st.session_state:
        st.session_state.cfg = load_config()
    return st.session_state.cfg


def _save_uploads(project: str, files) -> int:
    """Сохранить загруженные файлы во ВРЕМЕННУЮ папку проекта (для индексации).

    Файлы не входят в постоянное хранилище — после индексации их можно удалить
    (кнопка «Очистить временные файлы»). В базе остаются только чанки/токены.
    """
    up = project_paths(project)["uploads"]
    up.mkdir(parents=True, exist_ok=True)
    n = 0
    overwritten: list[str] = []
    for f in files or []:
        try:
            dest = up / f.name
            if dest.exists():
                overwritten.append(f.name)
            dest.write_bytes(f.getbuffer())
            n += 1
        except Exception as e:  # noqa: BLE001
            st.error(f"Не удалось сохранить {f.name}: {e}")
    if overwritten:
        st.warning("Перезаписаны одноимённые файлы: " + ", ".join(overwritten[:10])
                   + (" …" if len(overwritten) > 10 else "")
                   + ". Если это РАЗНЫЕ версии — переименуйте, иначе одна затрёт другую.")
    return n


def _workflow_state(project: str) -> dict:
    """Лёгкая сводка готовности ключевых модулей (М1/М2/М4).

    Только чтение JSON проекта — без импорта моделей и без сети, поэтому
    вызывается на каждый рендер дёшево. Даёт «где я / что делать дальше»,
    чтобы пользователь не терялся между шестью вкладками.
    """
    import json
    pp = project_paths(project)
    ws = {"m1": "○", "m2": "○", "m4": "○",
          "m1_txt": "", "m2_txt": "", "m4_txt": "", "next": ""}

    def _load(key):
        try:
            p = pp[key]
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        except Exception:  # noqa: BLE001
            return None

    # М1 — систематизация: есть карта разделов?
    inv = _load("inventory")
    files = (inv or {}).get("files", []) if isinstance(inv, dict) else []
    if files:
        unknown = sum(1 for it in files if it.get("section") in ("", "UNKNOWN"))
        recognized = len(files) - unknown
        ws["m1"] = "✅" if unknown == 0 else "◑"
        ws["m1_txt"] = (f"файлов {len(files)}, распознано {recognized}"
                        + (f", НЕ распознано {unknown}" if unknown else ""))

    # М2 — индексация: статус фоновой сборки RAG-базы
    stt = _load("index_state")
    if isinstance(stt, dict):
        s = stt.get("status", "idle")
        # «running» при мёртвом процессе (жёсткое убийство/выключение) иначе висел
        # бы вечным ⏳: проверяем возраст «пульса» (пишется каждые ~5 с) — чистое
        # чтение того же JSON, без импорта индексатора.
        if s == "running":
            try:
                from datetime import datetime as _dt
                _ts = stt.get("heartbeat") or stt.get("updated_at") or ""
                if (_dt.now() - _dt.fromisoformat(_ts)).total_seconds() > 120:
                    s = "error"
            except Exception:  # noqa: BLE001
                pass
        done, total = stt.get("done_files", 0), stt.get("total_files", 0)
        ws["m2"] = {"done": "✅", "running": "⏳", "paused": "⏸",
                    "error": "⚠", "idle": "○"}.get(s, "○")
        ws["m2_txt"] = {"done": f"проиндексировано {done} файл(ов)",
                        "running": f"идёт: {done}/{total}",
                        "paused": f"пауза: {done}/{total}",
                        "error": "прервалась — см. журнал"}.get(s, "")

    # М4 — ответы на замечания
    ans = _load("answers")
    lst = ans.get("answers", []) if isinstance(ans, dict) else []
    if lst:
        ws["m4"] = "✅"
        ws["m4_txt"] = f"ответов {len(lst)}"

    # Подсказка «следующий шаг» по фактическому состоянию
    if ws["m1"] == "○":
        ws["next"] = "▶ Начните с **М1**: загрузите файлы проектной документации."
    elif ws["m2"] == "⏳":
        ws["next"] = "⏳ Идёт индексация в **М2** — можно дождаться и перейти к М4."
    elif ws["m2"] == "⚠":
        ws["next"] = "⚠ Индексация в **М2** прервалась — откройте «🩺 Диагностику запуска»."
    elif ws["m2"] != "✅":
        ws["next"] = "▶ Дальше — **М2**: постройте RAG-базу (кнопка «Запустить индексацию»)."
    elif ws["m4"] == "○":
        ws["next"] = "▶ Дальше — **М4**: загрузите замечания и сформируйте ответы."
    else:
        ws["next"] = "✅ Основной путь пройден. **М5** — корректировка томов, **М6** — выгрузка для УПРЗА."
    return ws


# ─────────────────────────────── сайдбар ───────────────────────────────
def sidebar() -> tuple[str, str]:
    st.sidebar.title("🌍 СтройПроект")
    st.sidebar.caption(f"v{__version__} «{__codename__}»")

    projects = list_projects()
    mode = st.sidebar.radio("Проект", ["Выбрать", "Создать новый"],
                            horizontal=True, label_visibility="collapsed")
    if mode == "Создать новый" or not projects:
        name = st.sidebar.text_input("Название проекта", placeholder="ОПОЧКА-ДУБРОВКА 83-26С")
        if st.sidebar.button("Создать проект", disabled=not name):
            register_project(name)
            st.session_state.project = name
            st.rerun()
        project = st.session_state.get("project", name or "")
    else:
        idx = projects.index(st.session_state["project"]) if st.session_state.get("project") in projects else 0
        project = st.sidebar.selectbox("Проект", projects, index=idx)
        st.session_state.project = project

    # Тип объекта (влияет на состав разделов ПД по ПП-87)
    cfg = _cfg()
    cur_ot = st.session_state.get("object_type", cfg.get("object_type", "площадной"))
    object_type = st.sidebar.radio(
        "Тип объекта (ПП-87)", ["площадной", "линейный"],
        index=0 if cur_ot == "площадной" else 1,
        help="Меняет обязательный состав разделов ПД. У линейных объектов есть ТКР (Раздел 3).",
    )
    st.session_state.object_type = object_type

    fs_cur = int(cfg.get("ui.font_size", 19))
    fs_opts = [16, 17, 18, 19, 20, 22, 24]
    fs_new = st.sidebar.select_slider("Размер шрифта", options=fs_opts,
                                      value=fs_cur if fs_cur in fs_opts else 19)
    if int(fs_new) != fs_cur:
        cfg.set("ui.font_size", int(fs_new)); cfg.save(); st.rerun()

    st.sidebar.divider()
    with st.sidebar:
        C.ai_settings_panel(cfg)

    return project, object_type


# ─────────────────────────────── модули ───────────────────────────────
def _m1_ai_classify(project: str, object_type: str, unknown_files: list) -> None:
    """Доуточнить разделы нераспознанных файлов через выбранный ИИ (в т.ч. Ollama).
    Замечание 7мои: базовая систематизация — детерминированная (по именам, БЕЗ ИИ),
    поэтому смена провайдера её не меняла; теперь ИИ подключается этой функцией."""
    from pmoos.core.ai_providers import chat
    from pmoos.core.json_utils import extract_json_safe
    from pmoos.ingest.sections import required_sections, SURVEYS, section_name
    from pmoos.ingest.inventory import set_file_section
    cfg = _cfg()
    codes = [x["code"] for x in required_sections(object_type)] + [x["code"] for x in SURVEYS]
    legend = "\n".join(f"- {c}: {section_name(c)}" for c in codes)
    listing = "\n".join(f"{i + 1}. {f['rel']}" for i, f in enumerate(unknown_files))
    sys_msg = ("Ты помощник по составу проектной документации (ПП-87 РФ). По ИМЕНИ файла "
               "определи код раздела из допустимого списка. Отвечай ТОЛЬКО JSON-объектом "
               "вида {\"имя файла\": \"КОД\"}. Если уверенно определить нельзя — \"UNKNOWN\".")
    user = f"Допустимые коды разделов:\n{legend}\n\nФайлы:\n{listing}\n\nJSON:"
    try:
        out = chat(cfg, [{"role": "system", "content": sys_msg},
                         {"role": "user", "content": user}],
                   module="module1", role="extract", json_mode=True, max_tokens=1500)
        mapping = extract_json_safe(out, default={}, expect="object") or {}
    except Exception as e:  # noqa: BLE001
        st.error(f"ИИ-распознавание не удалось: {e}")
        return
    ok = 0
    for f in unknown_files:
        code = str(mapping.get(f["rel"]) or mapping.get(f.get("name", "")) or "").strip().upper()
        if code in codes:
            set_file_section(project, f["rel"], code)
            ok += 1
    if ok:
        st.success(f"ИИ определил разделы: {ok} из {len(unknown_files)}.")
    else:
        st.warning("ИИ не смог уверенно определить разделы — проставьте вручную ниже.")


def tab_m1(project: str, object_type: str) -> None:
    st.header("МОДУЛЬ 1 · Загрузка и систематизация ПД (ПП-87)")
    C.module_ai_selector(_cfg(), "module1")
    st.caption("Файлы проекта НЕ сохраняются: в базе остаются только карта разделов "
               "и (после М2) векторные чанки. Временные файлы можно удалить после индексации.")

    # Автосистематизация (замечание: «не видит загруженные разделы»): если файлы уже
    # лежат в папке проекта (загружены ранее или добавлены вручную), а карта разделов
    # отсутствует/устарела — строим её автоматически, без нажатия кнопок.
    from pmoos.ingest.inventory import load_inventory as _li, build_inventory as _bi
    from pmoos.ingest.loaders import SUPPORTED_EXT as _SUP
    _up = project_paths(project)["uploads"]
    # фильтруем ТЕМИ ЖЕ расширениями, что и build_inventory: посторонний файл
    # (например .tmp/.log) иначе давал вечное «карта устарела» → пересборка на
    # каждый rerun всего приложения
    _disk = (sorted(str(q.relative_to(_up)) for q in _up.rglob("*")
                    if q.is_file() and q.suffix.lower() in _SUP)
             if _up.exists() else [])
    _inv0 = _li(project)
    _invf = sorted(f.get("rel", "") for f in (_inv0 or {}).get("files", []))
    if _disk and _disk != _invf:
        _bi(project, object_type=object_type)
        st.info(f"Найдено файлов в проекте: {len(_disk)} — карта разделов обновлена автоматически.")
    elif _disk:
        st.caption(f"Файлов в проекте: **{len(_disk)}** · карта разделов актуальна.")

    files = st.file_uploader(
        "Загрузите файлы ПД (pdf / docx / xlsx). Можно перетащить много файлов.",
        type=["pdf", "docx", "xlsx", "xlsm", "txt", "md", "csv"],
        accept_multiple_files=True,
    )
    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("📥 Загрузить и систематизировать", disabled=not files, width='stretch'):
            n = _save_uploads(project, files)
            from pmoos.ingest.inventory import build_inventory
            build_inventory(project, object_type=object_type)
            st.success(f"Учтено файлов: {n}. Карта разделов обновлена.")
    with c2:
        if st.button("🔁 Пересобрать карту разделов", width='stretch'):
            from pmoos.ingest.inventory import build_inventory
            build_inventory(project, object_type=object_type)
            st.success("Карта разделов пересобрана.")
    with c3:
        if st.button("🧹 Очистить временные файлы", width='stretch',
                     help="Удалить загруженные файлы ПД из tmp_uploads. Карта разделов, "
                          "RAG-база и файлы замечаний (папка remarks/) сохраняются."):
            import shutil
            up = project_paths(project)["uploads"]
            if up.exists():
                shutil.rmtree(up, ignore_errors=True)
            st.success("Временные файлы удалены. Карта разделов и RAG-база сохранены.")

    with st.expander("📇 Контакты проектировщиков и экспертов"):
        from pmoos.ingest.contacts import extract_contacts, load_contacts, save_contacts
        _cd = load_contacts(project)
        cx1, cx2 = st.columns([1, 2])
        if cx1.button("🔎 Извлечь из разделов", key="m1_contacts_extract", width='stretch',
                      help="Читает первые страницы файлов ПД и файла замечаний; "
                           "собирает роли, ФИО, телефоны, email, организации."):
            with st.spinner("Извлечение контактов…"):
                _cd = extract_contacts(project, _cfg())
            st.success(f"Найдено: людей {len(_cd['люди'])}, "
                       f"организаций {len(_cd['организации'])}.")
        if _cd.get("организации"):
            cx2.caption("Организации: " + "; ".join(_cd["организации"]))
        _rows = _cd.get("люди") or [{"роль": "", "ФИО": "", "телефон": "",
                                     "email": "", "тип": "проектировщик", "файл": ""}]
        _ed = st.data_editor(_rows, num_rows="dynamic", width='stretch',
                             key="m1_contacts_table")
        if st.button("💾 Сохранить контакты", key="m1_contacts_save"):
            save_contacts(project, {"люди": [r for r in _ed
                                             if (r.get("ФИО") or r.get("роль"))],
                                    "организации": _cd.get("организации", [])})
            st.success("Сохранено: contacts.json")

    # ИИ-доуточнение нераспознанных файлов (по выбранному провайдеру модуля 1)
    _inv1 = _li(project)
    _unk = [f for f in (_inv1 or {}).get("files", []) if f.get("section") == "UNKNOWN"]
    if _unk:
        st.warning(
            f"⚠ Не определён раздел у **{len(_unk)}** файл(ов) — при ответах (М4) поиск их "
            f"не увидит. Что делать (любой способ): переименуйте по образцу «Раздел N. …», "
            f"задайте раздел вручную ниже в блоке «✏️», либо нажмите «🤖 Распознать разделы "
            f"с ИИ». Причина — базовое распознавание идёт по ИМЕНИ файла, а оно нетиповое."
        )
    u1, u2 = st.columns([2, 3])
    with u1:
        if st.button(f"🤖 Распознать разделы с ИИ (нераспознанных: {len(_unk)})",
                     disabled=not _unk, width='stretch', key="m1_ai_classify",
                     type="primary" if _unk else "secondary",
                     help="Отправляет ИМЕНА нераспознанных файлов выбранному провайдеру "
                          "(включая локальную Ollama) и проставляет коды разделов."):
            _m1_ai_classify(project, object_type, _unk)
            st.rerun()
    with u2:
        st.caption("Базовая систематизация — детерминированная (по именам файлов, без ИИ), "
                   "поэтому она одинакова для всех провайдеров. ИИ подключается кнопкой слева.")

    st.subheader("Карта разделов проектной документации")
    C.section_map(project, object_type)

    _exp_title = ("✏️ Подтвердить / исправить раздел и версию файла"
                  + (f" · без раздела: {len(_unk)}" if _unk else " (догадку не навязываем)"))
    with st.expander(_exp_title, expanded=bool(_unk)):
        from pmoos.ingest.inventory import load_inventory, set_file_section, set_file_version
        from pmoos.ingest.sections import required_sections, SURVEYS, section_name
        inv = load_inventory(project)
        if inv and inv.get("files"):
            st.caption(f"Тип объекта проекта: **{inv.get('object_type', object_type)}** "
                       f"(меняется слева; влияет на состав разделов и определение версий)")
            codes = [s["code"] for s in required_sections(object_type)] + [s["code"] for s in SURVEYS] + ["UNKNOWN"]
            for it in inv["files"]:
                cands = ", ".join(f"{c['code']}({c['score']})" for c in it.get("candidates", [])) or "—"
                cols = st.columns([3, 2, 2, 2])
                cols[0].write(f"📄 {it['name']}")
                cols[1].caption(f"кандидаты: {cands}")
                new = cols[2].selectbox(
                    "раздел", codes, index=codes.index(it["section"]) if it["section"] in codes else len(codes) - 1,
                    format_func=lambda c: f"{c} · {section_name(c)}" if c != "UNKNOWN" else "не определён",
                    key=f"sec_{it['rel']}", label_visibility="collapsed",
                )
                if new != it["section"]:
                    set_file_section(project, it["rel"], new)
                    st.rerun()
                cur_ver = it.get("version_override") or it.get("version_hint") or ""
                ver = cols[3].text_input("версия", value=cur_ver, key=f"ver_{it['rel']}",
                                         placeholder="версия", label_visibility="collapsed")
                if ver and ver != cur_ver:
                    set_file_version(project, it["rel"], ver)
                    st.rerun()
        else:
            st.info("Сначала загрузите файлы.")

    st.subheader("Версии разделов")
    C.version_map(project, object_type)
    # блок C.contacts_panel УДАЛЁН: он писал в contacts.json СВОЮ схему
    # (designers/experts) поверх схемы экспандера «📇 Контакты…» (люди/организации) —
    # сохранение одного блока стирало данные другого. Контакты — в экспандере выше.


def tab_m2(project: str, object_type: str) -> None:
    st.header("МОДУЛЬ 2 · RAG-база (индексация)")
    st.caption("Простыми словами: программа читает загруженные документы и строит по ним "
               "«поисковую память», по которой ИИ находит нужные места для ответов на "
               "замечания. Это разовая подготовка — делается один раз на проект.")
    st.caption("База Qdrant хранится отдельно от приложения — повторный запуск не "
               "переиндексирует уже загруженное (дедупликация по содержимому, "
               "стабильные ID чанков). Работает на обычном компьютере (CPU); "
               "видеокарта NVIDIA ускоряет, но не обязательна.")
    C.indexing_panel(project, object_type)


def tab_m3(project: str, object_type: str) -> None:
    st.header("МОДУЛЬ 3 · Граф связей разделов и каскад изменений")
    C.module_ai_selector(_cfg(), "module3")
    from pmoos.graph.dependency import build_and_save, to_vis
    from pmoos.graph.cascade import downstream

    st.caption("Модуль отвечает на вопрос: «если изменить раздел X — что ещё придётся "
               "пересчитать или поправить?». Связи построены по матрице ПП-87 и "
               "фактическому составу разделов этого проекта.")
    g = build_and_save(project)
    vis = to_vis(g)
    _lbl = {n["id"]: n.get("label", n["id"]) for n in vis["nodes"]}
    mm1, mm2 = st.columns(2)
    mm1.metric("Разделов в графе", g.number_of_nodes())
    mm2.metric("Связей «данные → использование»", g.number_of_edges())

    st.subheader("Что затронет изменение раздела")
    nodes = sorted(n["id"] for n in vis["nodes"])
    pick = st.selectbox("Если изменится…", nodes, key="m3_impact",
                        format_func=lambda c: f"{c} — {_lbl.get(c, c)}")
    if pick:
        res = downstream(project, [pick])
        if res["affected"]:
            st.markdown("**Потребуется перепроверить (по цепочке зависимостей):**")
            st.dataframe([{"Раздел": a["label"],
                           "Шагов от изменения": a["depth"],
                           "Цепочка": " → ".join(_lbl.get(x, x) for x in a["via"])}
                          for a in res["affected"]],
                         width='stretch', hide_index=True)
            st.info("Рекомендуемый порядок пересчёта: " +
                    " → ".join(_lbl.get(x, x) for x in res["order"]))
            try:
                from pmoos.graph.cascade import explain_cascade
                _exp = explain_cascade(project, [pick])
                if _exp:
                    st.caption(_exp)
            except Exception:  # noqa: BLE001
                pass
        else:
            st.success("Изменение этого раздела другие разделы не затрагивает.")

    with st.expander("Все связи между разделами (таблица)"):
        st.dataframe([{"Откуда (источник данных)": _lbl.get(e["from"], e["from"]),
                       "Куда (использует данные)": _lbl.get(e["to"], e["to"]),
                       "Какие данные передаются": e.get("title", "")}
                      for e in vis["edges"]],
                     width='stretch', hide_index=True)

    show_graph = st.toggle("Показать интерактивный граф (pyvis)", value=False, key="m3_show_graph")
    if show_graph:
        from pmoos.graph.dependency import write_vis_html
        try:
            html_path = write_vis_html(project, vis)
            html = Path(html_path).read_text(encoding="utf-8")
            try:
                components.html(html, height=620, scrolling=True)
            except Exception:
                _download(html_path)
        except Exception as e:  # noqa: BLE001
            st.caption(f"Не удалось построить интерактивный граф: {e}")
    st.divider()
    st.subheader("🧠 Накопительный граф знаний по всем проектам")
    st.caption("Копит, какая техника/ЗВ встречалась в каких разделах и проектах "
               "(растёт при приёме ответов и по кнопке ниже). Хранится на диске, без сервера.")
    from pmoos.graph.knowledge import update_from_project, stats, to_vis as kg_vis
    cols = st.columns([1, 2])
    if cols[0].button("➕ Обновить граф знаний из этого проекта", width='stretch'):
        kn = update_from_project(project)
        st.success(f"Добавлено сущностей: {kn['entities']}. Узлов: {kn['nodes']}, связей: {kn['edges']}.")
    s = stats()
    cols[1].write(f"Сейчас в графе знаний: **{s['nodes']}** узлов, **{s['edges']}** связей "
                  f"({', '.join(f'{k}: {v}' for k, v in s['by_kind'].items()) or '—'})")
    q = st.text_input("Поиск: в каких проектах встречалась техника/ЗВ",
                      placeholder="напр. экскаватор или азота диоксид")
    if q:
        from pmoos.graph.knowledge import projects_with_entity
        projs = projects_with_entity(q)
        st.write("Найдено в проектах: " + (", ".join(projs) if projs else "—"))


def _gdrive_direct(url: str):
    """Преобразует ссылку Google Drive (file/d/…, open?id=, uc?id=) в прямую."""
    m = re.search(r"drive\.google\.com/(?:file/d/([-\w]{10,})|open\?id=([-\w]{10,})"
                  r"|uc\?[^\s]*?id=([-\w]{10,}))", url)
    if m:
        fid = next(g for g in m.groups() if g)
        return f"https://drive.google.com/uc?export=download&id={fid}", fid
    return url, None


def _download_remarks_url(project: str, url: str) -> Path:
    """Скачивает файл замечаний по ссылке (https, в т.ч. Google Drive «по ссылке»)
    в постоянную папку remarks/ проекта."""
    import requests
    from urllib.parse import urlparse, unquote
    direct, fid = _gdrive_direct(url.strip())
    r = requests.get(direct, timeout=90, allow_redirects=True)
    if fid and "text/html" in (r.headers.get("Content-Type") or ""):
        m = re.search(r"confirm=([0-9A-Za-z_\-]+)", r.text)
        if m:  # большие файлы Drive требуют подтверждения
            r = requests.get(f"{direct}&confirm={m.group(1)}", timeout=180, allow_redirects=True)
    r.raise_for_status()
    ctype = (r.headers.get("Content-Type") or "").lower()
    if "text/html" in ctype:
        raise RuntimeError("по ссылке вернулась HTML-страница, а не файл. Для Google Drive "
                           "включите доступ «Все, у кого есть ссылка» и давайте ссылку на "
                           "ФАЙЛ (не на папку).")
    name = None
    cd = r.headers.get("Content-Disposition") or ""
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
    if m:
        name = unquote(m.group(1)).strip()
    if not name:
        name = Path(urlparse(direct).path).name or "замечания_по_ссылке"
        if "." not in Path(name).name:
            name += (".pdf" if "pdf" in ctype else
                     ".docx" if "wordprocessingml" in ctype else
                     ".doc" if "msword" in ctype else
                     ".xlsx" if "spreadsheetml" in ctype else ".bin")
    rdir = project_paths(project)["remarks_dir"]
    rdir.mkdir(parents=True, exist_ok=True)
    out = rdir / name
    out.write_bytes(r.content)
    if out.stat().st_size < 64:
        raise RuntimeError(f"скачан подозрительно маленький файл ({out.stat().st_size} байт) — "
                           f"проверьте доступ по ссылке.")
    return out


def tab_m4(project: str, object_type: str) -> None:
    st.header("МОДУЛЬ 4 · Ответы на замечания ПМООС")
    cfg = _cfg()
    C.module_ai_selector(cfg, "module4")

    # честное ожидание: реранжирование на CPU ощутимо дольше, чем на GPU —
    # предупреждаем ДО запуска (тот же индикатор, что в М2)
    try:
        from pmoos.core.device import resolve_device
        if resolve_device(cfg.get("embedding.device", "auto")) != "cuda":
            st.caption("Устройство: 🟠 CPU — поиск и реранжирование источников идут "
                       "медленнее, чем на видеокарте NVIDIA (на ~75 замечаний — до "
                       "десятков минут). Это нормально, ход виден по прогрессу.")
    except Exception:  # noqa: BLE001
        pass

    try:
        from pmoos.memory import kb_size
        n_kb = kb_size()
        if n_kb:
            st.caption(f"🧠 Память экспертизы: {n_kb} принятых ответов из прошлых проектов "
                       f"будут использованы как примеры (few-shot) при поиске ответов.")
    except Exception:  # noqa: BLE001
        pass

    rfile = st.file_uploader("Файл замечаний (docx/xlsx/pdf) — со словом «замечания» в имени удобнее",
                             type=["docx", "doc", "xlsx", "pdf", "txt"], key="remarks_up")
    remarks_path = None
    if rfile is not None:
        # ПОСТОЯННАЯ папка remarks/ — кнопка «Очистить временные файлы» её не трогает
        # (раньше файл жил в tmp_uploads и пропадал после очистки → «Package not found»)
        rdir = project_paths(project)["remarks_dir"]
        rdir.mkdir(parents=True, exist_ok=True)
        remarks_path = rdir / rfile.name
        _data = bytes(rfile.getbuffer())
        # пишем только если файл изменился: пока он лежит в uploader'e, каждый
        # rerun приложения иначе перезаписывал бы его на диск заново
        if not (remarks_path.exists() and remarks_path.stat().st_size == len(_data)):
            remarks_path.write_bytes(_data)
        try:
            _ok = remarks_path.stat().st_size == len(_data)
        except OSError:
            _ok = False
        if not _ok:
            st.error(f"Не удалось сохранить файл замечаний: {remarks_path}. "
                     f"Возможно, мешает антивирус — попробуйте ещё раз.")
            remarks_path = None
        else:
            st.caption(f"Файл замечаний сохранён: {remarks_path}")

    uc1, uc2 = st.columns([4, 1])
    with uc1:
        _url = st.text_input("…или ссылка на ФАЙЛ замечаний (https, в т.ч. Google Drive «по ссылке»)",
                             key="m4_url", placeholder="https://drive.google.com/file/d/…")
    with uc2:
        st.write("")
        if st.button("⬇️ Скачать", key="m4_url_dl", width='stretch', disabled=not _url):
            try:
                _p = _download_remarks_url(project, _url)
                st.session_state["m4_url_path"] = str(_p)
                st.success(f"Скачано: {_p.name}")
            except Exception as e:  # noqa: BLE001
                st.error(f"Не удалось скачать по ссылке: {e}")
    if remarks_path is None:
        _sp = st.session_state.get("m4_url_path")
        if _sp and Path(_sp).exists():
            remarks_path = Path(_sp)
            st.caption(f"Используется файл, скачанный по ссылке: {remarks_path.name}")

    c1, c2, c3 = st.columns(3)
    run1 = c1.button("① Найти ответы", width='stretch')
    run2 = c2.button("② Проверить правки", width='stretch')
    run3 = c3.button("③ Финальная проверка", width='stretch')

    if run1:
        from pmoos.pipeline.block1_answers import run_block1
        with st.spinner("Поиск ответов (retrieval + ИИ)…"):
            try:
                out = run_block1(project, cfg, remarks_path=remarks_path, object_type=object_type)
                st.success(f"Готово: {out.get('count', 0)} ответов.")
            except Exception as e:  # noqa: BLE001
                st.error(f"Ошибка блока 1: {e}")

    if run2:
        from pmoos.pipeline.block2_review import run_block2
        with st.spinner("Проверка расчётов/ссылок/нормативов…"):
            try:
                run_block2(project, cfg)
                st.success("Блок 2 завершён.")
            except Exception as e:  # noqa: BLE001
                st.error(f"Ошибка блока 2: {e}")

    if run3:
        from pmoos.pipeline.block3_final import run_block3
        with st.spinner("Финальная проверка раздела…"):
            try:
                out = run_block3(project, cfg, object_type=object_type)
                st.success(f"Готовность к экспертизе: {out.get('ready', '?')}")
                if out.get("summary"):
                    st.write(out["summary"])
            except Exception as e:  # noqa: BLE001
                st.error(f"Ошибка блока 3: {e}")

    st.divider()
    rc1, rc2 = st.columns([1, 3])
    if rc1.button("🗑 Сбросить ответы", key="m4_reset", width='stretch',
                  help="Полностью очистить предложенные ответы. "
                       "Журнал принятых решений (decisions.jsonl) сохраняется."):
        st.session_state["m4_reset_arm"] = True
    if st.session_state.get("m4_reset_arm"):
        rc2.warning("Сбросить ВСЕ предложенные ответы?")
        cc1, cc2 = rc2.columns(2)
        if cc1.button("Да, сбросить", key="m4_reset_yes", width='stretch'):
            from pmoos.pipeline.block1_answers import reset_answers
            reset_answers(project)
            st.session_state["m4_reset_arm"] = False
            st.rerun()
        if cc2.button("Отмена", key="m4_reset_no", width='stretch'):
            st.session_state["m4_reset_arm"] = False
            st.rerun()

    _render_answers(project)


def _render_answers(project: str) -> None:
    from pmoos.pipeline.block1_answers import (load_answers, set_decision,
                                               set_decisions, CATEGORIES)
    data = load_answers(project)
    answers = data.get("answers", [])
    if not answers:
        st.info("Ответы ещё не сформированы.")
        return

    ST_RU = {"proposed": "· предложен", "accepted": "✅ принят",
             "edited": "✎ правка", "rejected": "✗ отклонён"}
    ICON = {"accepted": "✅", "edited": "✎", "rejected": "✗", "proposed": "·"}

    st.subheader(f"Предложенные ответы ({len(answers)})")
    by_st = {k: sum(1 for a in answers if a.get("status", "proposed") == k) for k in ST_RU}
    m1c, m2c, m3c, m4c = st.columns(4)
    m1c.metric("Принято", by_st["accepted"]); m2c.metric("С правкой", by_st["edited"])
    m3c.metric("Отклонено", by_st["rejected"]); m4c.metric("Ожидают", by_st["proposed"])

    cat_counts: dict[str, int] = {}
    for a in answers:
        c = a.get("category") or "Правка по источникам"
        cat_counts[c] = cat_counts.get(c, 0) + 1
    st.caption("По типам: " + " · ".join(f"{c} — {n}" for c, n in cat_counts.items()))

    # предупреждение о слабой опоре на источники (dex-ревью): такие ответы ИИ мог
    # «выдумать» — их нужно проверить вручную (для экспертизы это критично)
    _low = [str(a.get("number", "")) for a in answers if a.get("low_support")]
    if _low:
        st.warning(f"⚠ {len(_low)} ответ(ов) без опоры на найденные фрагменты ПД "
                   f"(№ {', '.join(_low[:25])}) — ИИ мог ответить «от себя». Проверьте "
                   f"вручную: возможно, нужный раздел не проиндексирован (М2) или "
                   f"замечание сформулировано иначе, чем текст в документах.")

    f1, f2 = st.columns(2)
    cats = [c for c in CATEGORIES if c in cat_counts] + \
           [c for c in cat_counts if c not in CATEGORIES]
    sel_cat = f1.multiselect("Типы замечаний", cats, default=cats, key="m4_flt_cat")
    sel_st = f2.multiselect("Статусы", list(ST_RU), default=list(ST_RU),
                            format_func=lambda k: ST_RU[k], key="m4_flt_st")
    view = [a for a in answers
            if (a.get("category") or "Правка по источникам") in sel_cat
            and a.get("status", "proposed") in sel_st]

    def _cut(t: str, n: int) -> str:
        t = (t or "").replace("\n", " ").strip()
        return t if len(t) <= n else t[: n - 1] + "…"

    # №10-7/9: ВСЕ замечания видны сразу одной таблицей, без раскрытий
    st.dataframe(
        [{
            "№": a.get("number", ""),
            "Тип": a.get("category") or "—",
            "Том ООС": _cut(a.get("oos_volume", ""), 30) or "—",
            "Ст.": ICON.get(a.get("status", "proposed"), "·"),
            "Замечание": _cut(a.get("remark", ""), 115),
            "Ответ": _cut(a.get("user_answer") or a.get("answer", ""), 135),
            "Источник": _cut((a.get("sources") or [{}])[0].get("file", ""), 28),
        } for a in view],
        width='stretch', hide_index=True,
        height=min(560, 42 + 35 * max(1, len(view))))

    # №10-8: пакетная приёмка
    pb1, pb2, pb3 = st.columns(3)
    if pb1.button(f"✅ Принять показанные ({len(view)})", key="m4_acc_vis",
                  width='stretch', disabled=not view):
        set_decisions(project, [{"number": a["number"], "status": "accepted",
                                 "user_answer": a.get("user_answer") or None}
                                for a in view if a.get("status") != "accepted"])
        st.rerun()
    if pb2.button("✅ Принять ВСЕ", key="m4_acc_all", width='stretch'):
        st.session_state["m4_acc_all_arm"] = True
    if st.session_state.get("m4_acc_all_arm"):
        _low_all = [a for a in answers if a.get("low_support") or a.get("unsupported_refs")]
        st.warning(
            f"Принять ВСЕ {len(answers)} ответов (в т.ч. непроверенные)?"
            + (f" Из них **{len(_low_all)}** помечены как требующие проверки "
               f"(без опоры на источники / спорные ссылки) — они уйдут в экспертизу "
               f"и в память как принятые." if _low_all else ""))
        ya, na = st.columns(2)
        if ya.button("Да, принять все", key="m4_acc_all_yes", width='stretch'):
            set_decisions(project, [{"number": a["number"], "status": "accepted",
                                     "user_answer": a.get("user_answer") or None}
                                    for a in answers if a.get("status") != "accepted"])
            st.session_state["m4_acc_all_arm"] = False
            st.rerun()
        if na.button("Отмена", key="m4_acc_all_no", width='stretch'):
            st.session_state["m4_acc_all_arm"] = False
            st.rerun()
    if pb3.button("↩️ Снять решения (показанные)", key="m4_clr_vis",
                  width='stretch', disabled=not view):
        set_decisions(project, [{"number": a["number"], "status": "proposed",
                                 "user_answer": a.get("user_answer") or None}
                                for a in view])
        st.rerun()

    # экспорт ответов в CSV (открывается в Excel; UTF-8 BOM для кириллицы)
    import csv as _csv
    import io as _io
    _buf = _io.StringIO()
    _w = _csv.writer(_buf, delimiter=";")
    _w.writerow(["№", "Тип", "Том ООС", "Статус", "Замечание", "Ответ",
                 "Правка", "Достоверность", "Источник", "Проверить"])
    for a in answers:
        _w.writerow([a.get("number", ""), a.get("category", ""), a.get("oos_volume", ""),
                     a.get("status", ""), a.get("remark", ""),
                     a.get("user_answer") or a.get("answer", ""), a.get("correction", ""),
                     a.get("confidence", ""), (a.get("sources") or [{}])[0].get("file", ""),
                     "да" if (a.get("low_support") or a.get("unsupported_refs")) else ""])
    st.download_button("⬇️ Скачать ответы (CSV для Excel)", _buf.getvalue().encode("utf-8-sig"),
                       file_name=f"ответы_{project}.csv", mime="text/csv", key="m4_csv_dl")

    # провенанс генерации + журнал решений (доказуемый след для экспертизы)
    prov = data.get("provenance")
    dec_path = project_paths(project)["decisions"]
    if prov or dec_path.exists():
        with st.expander("ⓘ Как сгенерировано · журнал принятых решений"):
            if prov:
                st.caption(
                    "Снимок пайплайна: версия {v}, модель поиска top_k={tk}, "
                    "кандидатов {c}, реранк={rr} (окно {ml}), перефраз {ex}, "
                    "чанкинг «{cm}». По нему видно, чем отличался прогон при регрессии."
                    .format(v=prov.get("version", "—"), tk=prov.get("top_k", "—"),
                            c=prov.get("candidates", "—"), rr=prov.get("use_rerank", "—"),
                            ml=prov.get("reranker_max_length", "—"),
                            ex=prov.get("expansions", "—"), cm=prov.get("chunking_mode", "—")))
            if dec_path.exists():
                try:
                    _audit = dec_path.read_bytes()
                    st.download_button("⬇️ Скачать журнал решений (decisions.jsonl)", _audit,
                                       file_name="decisions.jsonl", mime="application/json",
                                       key="m4_audit_dl")
                    st.caption("Иммутабельный append-only журнал: что именно принято, "
                               "с текстом и источниками на момент решения.")
                except OSError:
                    pass

    st.markdown("#### Работа с отдельным замечанием")
    nums = [str(a.get("number", "")) for a in (view or answers)]
    pick = st.selectbox("Замечание №", nums, key="m4_sel")
    a = next((x for x in answers if str(x.get("number")) == str(pick)), None)
    if not a:
        return
    num = a.get("number", "?")
    head = f"**Замечание №{num}** · {a.get('category') or '—'}"
    if a.get("oos_volume"):
        head += f" · том ООС: {a['oos_volume']}"
    if a.get("low_support"):
        head += " · ⚠ без опоры на источники"
    st.markdown(head)
    st.markdown(a.get("remark", ""))
    if a.get("low_support"):
        st.warning("⚠ По этому замечанию в проиндексированной ПД не найдено "
                   "релевантных фрагментов — ответ ИИ может быть неточным. "
                   "Проверьте, проиндексирован ли нужный раздел (М2).")
    cons = a.get("consistency", {})
    if not cons.get("ok", True):
        st.warning("⚠ Возможные расхождения: " + "; ".join(cons.get("issues", [])))
    if a.get("error"):
        st.error(a["error"])
    txt = st.text_area("Ответ (можно отредактировать):",
                       value=a.get("user_answer") or a.get("answer", ""),
                       key=f"ans_{num}", height=140)
    if a.get("correction"):
        st.caption(f"Правка в ПМООС: {a['correction']}")
    if a.get("unsupported_refs") and not a.get("low_support"):
        st.warning("⚠ В ответе есть нормативы/вещества/техника, которых нет в найденных "
                   "источниках — достоверность снижена, проверьте ссылки вручную.")
    srcs = a.get("sources", [])
    if not srcs and a.get("retrieved_sources"):
        st.caption("ИИ не указал, какие фрагменты использовал. Ниже — что нашёл поиск "
                   "(НЕ подтверждено как использованное):")
        srcs = a.get("retrieved_sources", [])
    if srcs:
        st.dataframe([{"Раздел": x.get("section", ""), "Файл": x.get("file", ""),
                       "Место": x.get("loc", ""), "Релевантность": x.get("score", "")}
                      for x in srcs], width='stretch', hide_index=True)
    if a.get("cascade_text"):
        st.caption("Каскад: " + a["cascade_text"])
    d1, d2, d3 = st.columns(3)
    if d1.button("✅ Принять", key=f"acc_{num}"):
        set_decision(project, num, status="accepted", user_answer=txt or None)
        st.rerun()
    if d2.button("✎ Сохранить правку", key=f"edt_{num}"):
        set_decision(project, num, status="edited", user_answer=txt)
        st.rerun()
    if d3.button("✗ Отклонить", key=f"rej_{num}"):
        set_decision(project, num, status="rejected")
        st.rerun()


def tab_m5(project: str, object_type: str) -> None:
    st.header("МОДУЛЬ 5 · Корректировка ПМООС")
    st.caption("Формирует РЕАЛЬНО откорректированные тома ООС: правки вносятся в "
               "ИСХОДНЫЕ .docx и выделяются ЖЁЛТЫМ (по якорю «табл./п./раздел N» — "
               "рядом с местом, иначе в конец). Плюс «Ответы на замечания» для "
               "экспертизы (.docx/.xlsx). Томов может быть несколько.")

    ups = st.file_uploader("Исходные тома ООС (.docx / старый .doc) — можно несколько",
                           type=["docx", "doc"], accept_multiple_files=True, key="oos_up")
    vols_dir = project_paths(project)["root"] / "oos_src"
    src_paths: list[Path] = []
    if ups:
        vols_dir.mkdir(parents=True, exist_ok=True)
        for up in ups:
            p = vols_dir / up.name
            _buf = up.getbuffer()
            # не перезаписываем неизменённый том на каждый rerun приложения
            if not (p.exists() and p.stat().st_size == len(_buf)):
                p.write_bytes(_buf)
            src_paths.append(p)
        st.caption("Сохранено томов: " + ", ".join(p.name for p in src_paths))
    elif vols_dir.exists():
        src_paths = sorted(q for q in vols_dir.glob("*")
                           if q.suffix.lower() in (".docx", ".doc")
                           and not q.name.endswith(".converted.docx"))
        if src_paths:
            st.caption("Используются ранее загруженные тома: " +
                       ", ".join(p.name for p in src_paths))

    # Предпросмотр (dry-run) — контроль перед НЕОБРАТИМОЙ записью правок в тома.
    if src_paths and st.button("👁 Предпросмотр правок (без записи в файлы)",
                               key="m5_preview", width='stretch'):
        from pmoos.output.docx_writer import preview_corrections
        from pmoos.ingest.remarks import _convert_doc_with_word, _is_ole
        # та же подготовка, что при записи (.doc → .converted.docx): иначе
        # предпросмотр показывал бы ложное «в конец» для старых .doc-томов
        _prev_paths = []
        for p in src_paths:
            try:
                if p.suffix.lower() == ".doc" or _is_ole(p):
                    p = _convert_doc_with_word(p)
            except Exception:  # noqa: BLE001 — покажется как ошибка тома ниже
                pass
            _prev_paths.append(p)
        prev = preview_corrections(project, _prev_paths)
        if not prev.get("accepted"):
            st.warning("Нет принятых ответов — сначала примите их в Модуле 4.")
        else:
            st.caption(f"Будет внесено правок: **{prev['total']}** "
                       f"(принятых ответов: {prev['accepted']}). Исходные тома НЕ меняются "
                       f"до нажатия кнопки записи ниже.")
            for v in prev["volumes"]:
                with st.expander(f"📄 {v['volume']} — правок: {len(v['changes'])}", expanded=True):
                    if v.get("error"):
                        st.error(f"⚠ {v['error']}")
                    if v["changes"]:
                        st.dataframe([{"Замеч. №": c["number"], "Куда вставится": c["placed"],
                                       "Текст правки": (c.get("correction") or "")[:180]}
                                      for c in v["changes"]], width='stretch', hide_index=True)
                    else:
                        st.caption("Для этого тома правок нет.")

    g1, g2 = st.columns(2)
    if g1.button("📝 Откорректированные тома (правки жёлтым)", width='stretch',
                 key="m5_make_corr", disabled=not src_paths):
        from pmoos.output.docx_writer import write_corrected_volumes
        from pmoos.ingest.remarks import _convert_doc_with_word, _is_ole
        try:
            with st.status("Корректировка томов…", expanded=True) as sst:
                ready = []
                for p in src_paths:
                    if p.suffix.lower() == ".doc" or _is_ole(p):
                        sst.write(f"Конвертация {p.name} через Microsoft Word…")
                        p = _convert_doc_with_word(p)
                    ready.append(p)
                sst.write(f"Вставка правок в {len(ready)} том(а)…")
                outs = write_corrected_volumes(project, ready)
                sst.update(label="Готово", state="complete")
            if outs:
                for o in outs:
                    _download(o)
            else:
                st.warning("Нет принятых ответов — сначала примите их в Модуле 4 "
                           "(можно пакетно: «✅ Принять ВСЕ»).")
        except PermissionError:
            st.error("Файл тома открыт в Word (или защищён от записи). Закройте "
                     "документ в Word и нажмите кнопку ещё раз.")
        except Exception as e:  # noqa: BLE001
            _msg = str(e)
            if "Package not found" in _msg or "not a zip" in _msg.lower() or "BadZipFile" in _msg:
                st.error(f"Не удалось открыть том как .docx (файл повреждён или это не "
                         f"настоящий docx): {_msg}. Откройте его в Word и пересохраните "
                         f"как .docx, затем загрузите заново.")
            else:
                st.error(f"Ошибка корректировки: {_msg}")

    if g2.button("📄 Ответы на замечания для экспертизы (.docx/.xlsx)",
                 width='stretch', key="m5_make_table"):
        from pmoos.output.answers_table import build_answers_table_docx, build_answers_table_xlsx
        with st.spinner("Формирование таблицы ответов…"):
            p2 = build_answers_table_docx(project)
            p3 = build_answers_table_xlsx(project)
        st.success("Сформировано.")
        for p in (p2, p3):
            _download(p)

    with st.expander("🔬 Дополнительно"):
        if st.button("🧪 Выгрузить обучающие тройки (anchor/positive/negative)",
                     key="m5_triples",
                     help="Для будущего дообучения эмбеддера. Берёт принятые ответы."):
            from pmoos.output.training_export import export_triples
            r = export_triples(project)
            st.success(f"Сформировано троек: {r['count']}.")
            _download(r["path"])

    _list_outputs(project)


def tab_m6(project: str, object_type: str) -> None:
    st.header("МОДУЛЬ 6 · Выгрузка для УПРЗА «Эколог» / ИНТЕГРАЛ")
    st.caption("Модуль формирует ТОЛЬКО файлы для УПРЗА: источники выбросов + "
               "перечень ЗВ (коды) + задание на ввод. Геометрию источников и "
               "привязку значений заполняет инженер.")
    if st.button("📤 Сформировать выгрузку", width='stretch'):
        from pmoos.output.uprza_export import build_uprza_export, collect_emissions
        rows, extra = collect_emissions(project)
        paths = build_uprza_export(project)
        st.success(f"Готово. Распознано ЗВ: {len(rows)}.")
        if rows:
            st.dataframe([{"Код ЗВ": r["code"], "Наименование": r["name"]} for r in rows],
                         width='stretch', hide_index=True)
        for p in paths.values():
            _download(p)

    _list_outputs(project)


# ─────────────────────────────── утилиты вывода ───────────────────────────────
def _uid() -> int:
    """Монотонный счётчик за один рендер — гарантирует уникальные ключи виджетов."""
    n = st.session_state.get("_uid", 0)
    st.session_state["_uid"] = n + 1
    return n


def _download(path) -> None:
    path = Path(path)
    if not path.exists():
        return
    with path.open("rb") as f:
        st.download_button(f"⬇️ {path.name}", f.read(), file_name=path.name,
                           key=f"dl_{_uid()}_{path.name}")


def _list_outputs(project: str) -> None:
    out = project_paths(project)["out"]
    if out.exists():
        files = sorted(out.iterdir())
        if files:
            with st.expander(f"📁 Файлы проекта в out/ ({len(files)})"):
                for p in files:
                    _download(p)


# ─────────────────────────────── main ───────────────────────────────
def main() -> None:
    st.set_page_config(page_title=PAGE_TITLE, page_icon=PAGE_ICON, layout="wide")
    st.session_state["_uid"] = 0  # сброс счётчика ключей на каждый рендер
    apply_font_css(_cfg())  # размер шрифта из config (ui.font_size)
    project, object_type = sidebar()
    if not project:
        st.info("Создайте или выберите проект слева, чтобы начать.")
        return

    # смена проекта: сбросить «взведённые» подтверждения опасных действий и
    # скачанный файл замечаний — иначе подтверждение/путь «переезжали» на другой
    # проект (риск сброса ответов или переиндексации не того проекта)
    if st.session_state.get("_last_project") != project:
        for _k in ("m4_reset_arm", "m4_acc_all_arm", "idx_reindex_arm", "m4_url_path"):
            st.session_state.pop(_k, None)
        st.session_state["_last_project"] = project

    st.title(f"Проект: {project}")

    # Индикатор прохождения workflow: ✅ готово · ◑ частично · ⏳ идёт ·
    # ⚠ ошибка · ○ не начато. Виден прямо на вкладках + подсказка «что дальше».
    ws = _workflow_state(project)
    detail = " · ".join(
        f"{m}: {ws[k]}" for m, k in (("М1", "m1_txt"), ("М2", "m2_txt"), ("М4", "m4_txt")) if ws[k]
    )
    st.caption(ws["next"] + (f"    ·    {detail}" if detail else ""))

    tabs = st.tabs([
        f"{ws['m1']} М1 · Систематизация", f"{ws['m2']} М2 · Индексация",
        "М3 · Граф связей", f"{ws['m4']} М4 · Ответы",
        "М5 · Корректировка", "М6 · УПРЗА",
    ])
    with tabs[0]:
        tab_m1(project, object_type)
    with tabs[1]:
        tab_m2(project, object_type)
    with tabs[2]:
        tab_m3(project, object_type)
    with tabs[3]:
        tab_m4(project, object_type)
    with tabs[4]:
        tab_m5(project, object_type)
    with tabs[5]:
        tab_m6(project, object_type)


if __name__ == "__main__":
    main()
