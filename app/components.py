"""Переиспользуемые компоненты Streamlit для СтройПроект.

Здесь сосредоточены: панель настроек ИИ (с авто-сменой модели по провайдеру и
списком локальных моделей Ollama), карта разделов (таблица + версии), панель
индексации (прогресс/пауза/возобновление/фон) и контакты.
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

PROVIDERS = ["deepseek", "openai", "gemini", "anthropic", "kimi", "mistral", "ollama"]
MODULES = [
    ("module1", "М1 · Систематизация ПД"),
    ("module3", "М3 · Граф связей"),
    ("module4", "М4 · Ответы на замечания"),
]
PROVIDER_LABEL = {
    "deepseek": "DeepSeek", "openai": "OpenAI (GPT)", "gemini": "Google Gemini",
    "anthropic": "Anthropic (Claude)", "kimi": "Kimi (Moonshot)",
    "mistral": "Mistral", "ollama": "Ollama (локально)",
}

# Известные модели по провайдерам (пресеты; всегда можно ввести свою вручную).
KNOWN_MODELS = {
    "deepseek": ["deepseek-chat", "deepseek-reasoner"],
    "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "o3-mini"],
    "gemini": ["gemini-1.5-pro", "gemini-1.5-flash", "gemini-2.0-flash"],
    "anthropic": ["claude-3-5-sonnet-latest", "claude-3-7-sonnet-latest", "claude-3-5-haiku-latest"],
    "kimi": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
    "mistral": ["mistral-large-latest", "mistral-small-latest", "open-mistral-nemo"],
}
_MANUAL = "✍️ ввести вручную…"


def _guarded_select(label: str, options: list, *, key: str, current,
                    format_func=None, label_visibility: str = "visible"):
    """Селект, который не «воюет» с другими виджетами той же настройки:
    • пишет в конфиг ТОЛЬКО когда пользователь реально изменил ЭТОТ виджет;
    • если настройку поменяли в другом месте (сайдбар/вкладка) — виджет сам
      синхронизируется под текущее значение, ничего не затирая.
    Возвращает (choice, user_changed)."""
    akey = "_applied_" + key
    if (akey in st.session_state
            and current in options
            and st.session_state.get(key) != current
            and st.session_state.get(key) == st.session_state.get(akey)):
        st.session_state[key] = current  # внешнее изменение конфига → синхронизация
    kw = dict(key=key, label_visibility=label_visibility)
    if format_func is not None:
        kw["format_func"] = format_func
    idx = options.index(current) if current in options else 0
    choice = st.selectbox(label, options, index=idx, **kw)
    user_changed = (akey in st.session_state) and (choice != st.session_state[akey])
    st.session_state[akey] = choice
    return choice, user_changed


def _set_provider_models(cfg, provider: str, model: str, roles) -> None:
    for r in roles:
        cfg.set(f"ai.providers.{provider}.{r}", model)
    cfg.save()


def _model_select(cfg, provider: str, module: str, *, roles, label: str, options, key: str) -> None:
    """Выбор модели провайдера: пресеты / установленные (Ollama) / ввод вручную."""
    cur = cfg.model_for(provider, roles[0])
    opts = [o for o in options]
    if cur and cur not in opts:
        opts.insert(0, cur)
    opts.append(_MANUAL)
    choice, user_changed = _guarded_select(label, opts, key=key, current=cur)
    if choice == _MANUAL:
        manual = st.text_input("Название модели", value="", key=key + "_manual",
                               placeholder="точное имя модели у провайдера")
        if manual and manual != cur:
            _set_provider_models(cfg, provider, manual, roles)
            st.success(f"Модель сохранена: {manual}")
    elif user_changed and choice != cur:
        _set_provider_models(cfg, provider, choice, roles)


# ─────────────────────────────── НАСТРОЙКИ ИИ ───────────────────────────────
def module_ai_selector(cfg, module: str, *, title: str | None = None) -> None:
    """Компактный выбор провайдера/модели/ключа ПРЯМО во вкладке модуля
    (замечание: «модель по модулям выбирается в соответствующих вкладках»).
    Заголовок показывает активную связку провайдер·модель — видно без разворота."""
    from pmoos.config import write_env_key
    from pmoos.core.ollama_utils import ollama_available, list_installed_models

    cur = cfg.resolve_provider(module)
    if title is None:
        _mdl = cfg.model_for(cur, "answer") or "модель не задана"
        _key = "" if (cfg.has_key(cur) or cur == "ollama") else " · ⚠ нет ключа"
        title = f"🤖 ИИ этого модуля: {PROVIDER_LABEL.get(cur, cur)} · {_mdl}{_key}"
    with st.expander(title, expanded=not (cfg.has_key(cur) or cur == "ollama")):
        opts = ["(по умолчанию)"] + PROVIDERS
        override = cfg.get(f"ai.modules.{module}.provider")
        current = override if override in PROVIDERS else "(по умолчанию)"
        choice, user_changed = _guarded_select(
            "Провайдер для модуля", opts, key=f"msel_{module}", current=current,
            format_func=lambda p: PROVIDER_LABEL.get(p, p) if p != "(по умолчанию)" else "(по умолчанию)",
        )
        if user_changed:
            if choice == "(по умолчанию)":
                cfg.set(f"ai.modules.{module}", {}); cfg.save()
            else:
                cfg.set(f"ai.modules.{module}.provider", choice); cfg.save()
        provider = cfg.default_provider() if choice == "(по умолчанию)" else choice

        st.caption(f"Активный провайдер: **{PROVIDER_LABEL.get(provider, provider)}**")

        # ── выбор МОДЕЛИ провайдера (пресеты / установленные / вручную) ──
        if provider == "ollama":
            if ollama_available():
                installed = list_installed_models()
                if installed:
                    _model_select(cfg, provider, module,
                                  roles=("answer", "review", "extract", "expand"),
                                  label="Локальная модель Ollama (из установленных)",
                                  options=installed, key=f"mmodel_{module}")
                else:
                    st.warning("Ollama запущена, но модели не найдены. Напр.: `ollama pull qwen2.5:7b-instruct`")
            else:
                st.warning("Ollama не обнаружена на :11434. Запустите `ollama serve` "
                           "(или задайте адрес в переменной OLLAMA_HOST).")
        else:
            _model_select(cfg, provider, module, roles=("answer", "review"),
                          label="Модель (ответы и проверка)",
                          options=KNOWN_MODELS.get(provider, []), key=f"mmodel_{module}")
            with st.expander("🛠 Модели вспомогательных ролей (парсинг / расширение запросов)"):
                _model_select(cfg, provider, module, roles=("extract",),
                              label="Парсинг замечаний (extract)",
                              options=KNOWN_MODELS.get(provider, []), key=f"mmodel_{module}_ex")
                _model_select(cfg, provider, module, roles=("expand",),
                              label="Расширение запросов (expand)",
                              options=KNOWN_MODELS.get(provider, []), key=f"mmodel_{module}_xp")
            if cfg.has_key(provider):
                st.caption("Ключ API задан ✓")
            else:
                val = st.text_input(f"Ключ API {PROVIDER_LABEL.get(provider, provider)} (ввести вручную)",
                                    value="", type="password", key=f"mkey_{module}")
                if val:
                    _envp = write_env_key(provider, val)
                    st.success(f"Ключ сохранён: {_envp}"); st.rerun()


def ai_settings_panel(cfg) -> None:
    """Сайдбар «Настройки ИИ» — КОМПАКТНЫЙ (редизайн v0.22).

    Принцип «одно место для одной настройки»: в сайдбаре — только ГЛОБАЛЬНЫЕ
    вещи (провайдер/модель по умолчанию, ключи, статус локальных моделей) и
    read-only сводка по модулям. Настройка ИИ конкретного модуля — в его
    вкладке (блок «ИИ для этого модуля»), а не здесь: дублирование селекторов
    в двух местах путало пользователя («по моделям запутано всё»)."""
    from pmoos.config import write_env_key
    from pmoos.core.ollama_utils import ollama_available, list_installed_models
    from pmoos.core.model_cache import model_status

    st.subheader("⚙️ Настройки ИИ")

    # 1) провайдер по умолчанию + его модель — два контрола, не больше
    default_prov = cfg.default_provider()
    new_default = st.selectbox(
        "Провайдер по умолчанию", PROVIDERS,
        index=PROVIDERS.index(default_prov) if default_prov in PROVIDERS else 0,
        format_func=lambda p: PROVIDER_LABEL.get(p, p),
        help="Используется всеми модулями, у которых нет своей настройки. "
             "Свою настройку модуля задавайте в его вкладке — блок «ИИ для этого модуля».",
    )
    if new_default != default_prov:
        cfg.set("ai.default_provider", new_default)
        cfg.save()
        st.rerun()

    if new_default == "ollama":
        if ollama_available():
            inst = list_installed_models()
            if inst:
                _model_select(cfg, "ollama", "default",
                              roles=("answer", "review", "extract", "expand"),
                              label="Модель Ollama (из установленных)",
                              options=inst, key="sb_model_default")
            else:
                st.warning("Ollama запущена, но модели не найдены: `ollama pull qwen2.5:7b-instruct`")
        else:
            st.warning("Ollama не обнаружена на :11434 — запустите `ollama serve`.")
    else:
        _model_select(cfg, new_default, "default", roles=("answer", "review"),
                      label="Модель (ответы/проверка)",
                      options=KNOWN_MODELS.get(new_default, []), key="sb_model_default")
        if not cfg.has_key(new_default):
            st.warning(f"Нет ключа {PROVIDER_LABEL.get(new_default, new_default)} — задайте в «🔑 Ключи API» ниже.")

    # 2) read-only сводка «кто чем отвечает» (менять — во вкладках модулей)
    st.markdown("**Кто чем отвечает:**")
    for mod, label in MODULES:
        prov = cfg.resolve_provider(mod)
        own = bool(cfg.get(f"ai.modules.{mod}.provider"))
        mark = "✓" if cfg.has_key(prov) else "⚠ нет ключа"
        st.caption(f"{label.split(' · ')[0]} · {PROVIDER_LABEL.get(prov, prov)} · "
                   f"`{cfg.model_for(prov, 'answer')}` {mark}"
                   + (" · своя" if own else ""))
    st.caption("↳ изменить ИИ модуля — в его вкладке, блок «🤖 ИИ для этого модуля»")

    # 3) ключи API (пишутся в data_dir/.env через dotenv.set_key)
    with st.expander("🔑 Ключи API"):
        for prov in PROVIDERS:
            if prov == "ollama":
                continue
            has = cfg.has_key(prov)
            val = st.text_input(
                f"{PROVIDER_LABEL[prov]}" + (" ✅" if has else ""),
                value="", type="password",
                placeholder=("ключ задан — можно заменить" if has else "не задан"),
                key=f"key_{prov}",
            )
            if val:
                _envp = write_env_key(prov, val)
                st.success(f"Ключ {PROVIDER_LABEL[prov]} сохранён: {_envp}")
                st.rerun()

    # 4) статус локальных моделей — одной строкой (без лишнего разворота)
    emb = cfg.get("embedding.model", "BAAI/bge-m3")
    rer = cfg.get("reranker.model", "BAAI/bge-reranker-v2-m3")
    parts = []
    for name in (emb, rer):
        cached = model_status(name).get("cached")
        parts.append(f"{name.split('/')[-1]} {'✅' if cached else '⬇️'}")
    st.caption("📦 Локальные модели: " + " · ".join(parts) +
               ("" if all(model_status(n).get("cached") for n in (emb, rer))
                else " — скачаются в Модуле 2"))


# ─────────────────────────────── КАРТА РАЗДЕЛОВ ───────────────────────────────
def section_map(project: str, object_type: str) -> None:
    from pmoos.ingest.inventory import section_overview

    rows = section_overview(project, object_type)
    table = [{
        "": "✅" if r["present"] else "—",
        "№ ПП-87": r.get("num", ""),
        "Код": r["code"],
        "Раздел": r["name"] + (" (доп.)" if r.get("extra") else ""),
        "Файлов": r["n_files"],
    } for r in rows]
    st.dataframe(table, width='stretch', hide_index=True)

    present = sum(1 for r in rows if r["present"])
    st.caption(f"Присутствует разделов: {present} · отсутствует обязательных: "
               f"{sum(1 for r in rows if not r['present'] and not r.get('extra'))}")


def version_map(project: str, object_type: str) -> None:
    from pmoos.versioning.versions import analyze_versions, set_current_version

    vers = analyze_versions(project, object_type=object_type)
    groups = vers.get("groups", {})
    multi = {k: g for k, g in groups.items() if len(g.get("versions", [])) > 1}
    if not multi:
        st.info("Несколько версий одного раздела не обнаружено.")
        return
    st.caption("Для разделов с несколькими версиями отметьте актуальную:")
    from pmoos.ingest.sections import section_name
    for gkey, g in multi.items():
        st.markdown(f"**{section_name(g.get('section',''))}** — «{g.get('base','')}»")
        files = [v["file"] for v in g["versions"]]
        cur = g.get("current_file") or files[-1]
        labels = {v["file"]: f"{v.get('label','')} · {v.get('date') or 'без даты'} · {v['file']}"
                  for v in g["versions"]}
        chosen = st.radio("версия", files, index=files.index(cur) if cur in files else 0,
                          format_func=lambda f: labels.get(f, f), key=f"ver_{gkey}",
                          label_visibility="collapsed")
        if chosen != cur:
            set_current_version(project, gkey, chosen)
            st.success(f"Актуальная версия: {chosen}")


# ─────────────────────────────── ИНДЕКСАЦИЯ ───────────────────────────────
def indexing_panel(project: str, object_type: str) -> None:
    from pmoos.index.indexer import (
        progress_summary, start_background, request_pause, clear_pause, is_running,
    )

    # Статус локальных моделей + «скачать всё сразу» (замечание пользователя:
    # «надо скачивать сразу все модели»). Ход скачивания — в журнале ниже.
    from pmoos.config import load_config as _lc
    from pmoos.core.model_cache import model_status
    _c = _lc()
    _emb = str(_c.get("embedding.model", _c.get("embeddings.model", "BAAI/bge-m3")))
    _rer = str(_c.get("reranker.model", "BAAI/bge-reranker-v2-m3"))
    _sts = [model_status(_emb), model_status(_rer)]
    _line = " · ".join(f"`{x['model']}` {'✅' if x['cached'] else '⬇️ не скачана'}" for x in _sts)
    # устройство (GPU/CPU) — предупреждаем, что на CPU индексация в разы медленнее
    from pmoos.core.device import resolve_device
    _dev = resolve_device(_c.get("embedding.device", "auto"))
    mc1, mc2 = st.columns([3, 2])
    if _dev == "cuda":
        mc1.caption(f"Устройство: 🟢 GPU (CUDA) · Локальные модели: {_line}")
    else:
        mc1.caption(f"Устройство: 🟠 CPU (GPU не найден — индексация будет в разы "
                    f"медленнее) · Локальные модели: {_line}")
    if not all(x["cached"] for x in _sts):
        if mc2.button("⬇️ Скачать все модели сейчас", key="idx_dl_models", width='stretch',
                      help="Скачивает сразу обе модели (bge-m3 ~2.3 ГБ + reranker ~1.1 ГБ) "
                           "в фоне; ход загрузки — в «Журнале индексации» ниже."):
            from pmoos.index.indexer import start_prefetch_background
            start_prefetch_background(project)
            st.rerun()
    else:
        mc2.caption("✅ обе модели в кэше")

    s = progress_summary(project)
    running = s["running"]

    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        if st.button("▶ Индексировать", disabled=running, width='stretch',
                     type="primary"):
            pid = start_background(project, object_type=object_type)
            if pid:
                st.toast(f"Фоновый процесс запущен (pid {pid})")
            st.rerun()
    with c2:
        if st.button("⏸ Пауза", disabled=not running, width='stretch',
                     help="Мягкая пауза: текущий файл дорабатывается, затем процесс останавливается."):
            request_pause(project)
            st.rerun()
    with c3:
        if st.button("⏹ Стоп", disabled=not running, width='stretch', key="idx_stop",
                     help="Немедленно прервать индексацию. Прогресс по готовым файлам "
                          "сохраняется; «⏯ Продолжить» возобновит с места остановки."):
            from pmoos.index.indexer import stop_indexing
            stop_indexing(project)
            st.rerun()
    with c4:
        if st.button("⏯ Продолжить", disabled=running, width='stretch'):
            clear_pause(project)
            start_background(project, object_type=object_type)
            st.rerun()
    with c5:
        if st.button("🔄 Обновить", width='stretch'):
            st.rerun()

    st.progress(min(1.0, s["percent"] / 100.0),
                text=f"{s['percent']}% · файлов {s['files_done']}/{s['files_total']} · "
                     f"чанков {s['chunks_done']}")
    badge = {"running": "🟢 выполняется", "paused": "🟡 пауза", "done": "✅ завершено",
             "error": "🔴 ошибка", "idle": "⚪ не запускалось"}.get(s["status"], s["status"])
    st.write(f"Статус: {badge}" + (f" · {s.get('current_file','')}" if s.get("current_file") else ""))
    if s["status"] == "running":
        st.caption(f"Пульс процесса: {s.get('heartbeat_age', '—')} с назад "
                   f"(норма ≤ 10 с; статус обновляйте кнопкой «Обновить»)")
    if s.get("message"):
        if s["status"] == "error":
            st.error(s["message"])
        elif s["status"] == "done":
            st.success(s["message"])
        else:
            st.info(s["message"])
    # режим чанкинга — прямо в интерфейсе (раньше был только config.yaml — барьер
    # для не-программиста). Смена пишется в конфиг; действует после переиндексации.
    _mode = str(_c.get("chunking.mode", "char"))
    _MODES = {"char": "🔤 По символам (базовый, проверенный)",
              "semantic": "🧠 По смыслу (по пунктам НПА, точнее — требует замера)"}
    _mkeys = list(_MODES)
    _sel = st.selectbox(
        "Режим нарезки документов на фрагменты",
        _mkeys, index=_mkeys.index(_mode) if _mode in _mkeys else 0,
        format_func=lambda k: _MODES[k], key="idx_chunk_mode",
        help="«По смыслу» режет по границам пунктов НПА и заголовков — обычно точнее "
             "для экспертизы, но требует переиндексации с нуля. Сначала замерьте "
             "качество на scripts/eval_golden.py ДО и ПОСЛЕ (A/B).")
    if _sel != _mode:
        _c.set("chunking.mode", _sel); _c.save()
        st.session_state.pop("cfg", None)  # чтобы весь интерфейс подхватил новый конфиг
        st.warning("Режим сохранён. Он вступит в силу только после «♻ Переиндексировать "
                   "заново» (кнопка ниже) — иначе дедупликация пропустит уже загруженное.")
        _mode = _sel
    _mode_ru = _MODES[_mode]
    rc1, rc2 = st.columns([2, 3])
    if rc1.button("♻ Переиндексировать заново", disabled=running, width='stretch',
                  key="idx_reindex",
                  help="Удаляет базу проекта и индексирует ВСЁ заново. Нужно после смены "
                       "режима чанкинга (иначе дедупликация пропустит уже загруженные файлы). "
                       "Для A/B: замерьте scripts/eval_golden.py run ДО и ПОСЛЕ."):
        st.session_state["idx_reindex_arm"] = True
    if st.session_state.get("idx_reindex_arm"):
        rc2.warning(f"Стереть базу проекта и переиндексировать заново? Режим чанкинга: **{_mode_ru}**.")
        yy, nn = rc2.columns(2)
        if yy.button("Да, с нуля", key="idx_reindex_yes", width='stretch'):
            start_background(project, object_type=object_type, reindex=True)
            st.session_state["idx_reindex_arm"] = False
            st.toast("Переиндексация с нуля запущена")
            st.rerun()
        if nn.button("Отмена", key="idx_reindex_no", width='stretch'):
            st.session_state["idx_reindex_arm"] = False
            st.rerun()
    d1, d2 = st.columns([1, 3])
    with d1:
        if st.button("🛑 Сбросить статус", width='stretch', key="idx_reset",
                     help="Если статус «завис» — сбросьте и запустите индексацию заново. "
                          "Прогресс по уже обработанным файлам сохраняется."):
            from pmoos.index.indexer import reset_state
            reset_state(project)
            st.rerun()
    with d2:
        st.caption("Индексация идёт в фоне и переживает закрытие вкладки; пауза/возобновление "
                   "переживают перезапуск. Первый запуск скачивает модель bge-m3 (~2.3 ГБ) — "
                   "ход загрузки виден в журнале ниже; при сбое там же будет точная причина.")

    from pmoos.index.indexer import log_tail, log_path
    with st.expander("📜 Журнал индексации (index_log.txt)"):
        jc1, jc2 = st.columns([3, 1])
        jc1.caption("Журнал обнуляется при каждом новом запуске индексации.")
        if jc2.button("🧽 Очистить", key="idx_clear_log", width='stretch'):
            try:
                log_path(project).write_bytes(b"")
            except OSError:
                pass
            st.rerun()
        tail = log_tail(project, 60)
        if tail:
            st.code(tail, language="text")
        else:
            st.caption("Журнал пока пуст — появится после запуска индексации.")
        # путь показываем как code: в caption(markdown) «\.» съедался и путь
        # выглядел неправильно (C:\Users\Имя.pmoos-rag вместо Имя\.pmoos-rag)
        st.code(str(log_path(project)), language="text")

    with st.expander("🩺 Диагностика запуска (если индексация «не стартует»)"):
        st.caption("Только проверяет окружение и показывает причину — ничего не чинит: "
                   "найдены ли файлы, запускается ли дочерний python, пишется ли состояние.")
        if st.button("Проверить окружение", key="idx_diag", width='stretch'):
            import subprocess as _sp
            import sys as _sys
            from pmoos.paths import project_paths as _pp, APP_ROOT as _AR
            from pmoos.index.indexer import _iter_source_files as _isf, read_state as _rs
            checks: list[tuple[str, bool, str]] = []
            up = _pp(project)["uploads"]
            files = _isf(up) if up.exists() else []
            checks.append(("Файлы для индексации", bool(files),
                           f"{len(files)} шт. в {up}"))
            try:
                r = _sp.run([_sys.executable, "-c", "print('ok')"], cwd=str(_AR),
                            capture_output=True, timeout=30)
                ok_py = (r.returncode == 0 and b"ok" in r.stdout)
                checks.append(("Запуск дочернего python", ok_py,
                               _sys.executable if ok_py else (r.stderr or b"").decode("utf-8", "replace")[:200]))
            except Exception as e:  # noqa: BLE001
                checks.append(("Запуск дочернего python", False,
                               f"{e} — вероятно, блокирует антивирус"))
            try:
                r2 = _sp.run([_sys.executable, "-c", "import pmoos.index.indexer; print('ok')"],
                             cwd=str(_AR), capture_output=True, timeout=60)
                ok_mod = (r2.returncode == 0)
                checks.append(("Импорт модуля индексатора", ok_mod,
                               "ok" if ok_mod else (r2.stderr or b"").decode("utf-8", "replace")[-300:]))
            except Exception as e:  # noqa: BLE001
                checks.append(("Импорт модуля индексатора", False, str(e)))
            try:
                stt = _rs(project)
                checks.append(("Файл состояния читается", True,
                               f"status={stt.get('status')}"))
            except Exception as e:  # noqa: BLE001
                checks.append(("Файл состояния читается", False, str(e)))
            # GPU/VRAM — частая причина сбоев на 8-ГБ ноутбуке
            try:
                import torch as _torch
                if _torch.cuda.is_available():
                    free_b, total_b = _torch.cuda.mem_get_info()
                    checks.append(("Видеокарта (GPU)", True,
                                   f"{_torch.cuda.get_device_name(0)} · свободно "
                                   f"{free_b/2**30:.1f}/{total_b/2**30:.1f} ГБ VRAM"))
                else:
                    checks.append(("Видеокарта (GPU)", True,
                                   "CUDA недоступна — работа на CPU (в разы медленнее, но рабочая)"))
            except Exception as e:  # noqa: BLE001
                checks.append(("Видеокарта (GPU)", True, f"не удалось определить: {e}"))
            # локальные модели в кэше
            try:
                _mok = all(model_status(n).get("cached") for n in (_emb, _rer))
                checks.append(("Модели bge (эмбеддер+реранкер)", _mok,
                               "обе в кэше" if _mok else "не скачаны — кнопка «Скачать все модели»"))
            except Exception as e:  # noqa: BLE001
                checks.append(("Модели bge", False, str(e)))
            # целостность базы Qdrant + число точек
            try:
                from pmoos.index.vectorstore import VectorStore as _VS
                from pmoos.index.embeddings import Embedder as _Emb
                _vs = _VS(_c, dim=_Emb(_c).dim)
                _cnt = int(_vs.count(project))
                try:
                    _vs.close()
                except Exception:  # noqa: BLE001
                    pass
                checks.append(("База Qdrant читается", True,
                               f"чанков в проекте: {_cnt}"
                               + (" (пусто — запустите индексацию)" if _cnt == 0 else "")))
            except Exception as e:  # noqa: BLE001
                checks.append(("База Qdrant читается", False,
                               f"{e} — возможно, повреждена; поможет «Переиндексировать заново»"))
            # свободное место на диске данных
            try:
                import shutil as _sh
                from pmoos.paths import data_root as _dr
                _free = _sh.disk_usage(str(_dr())).free / 2**30
                checks.append(("Свободно на диске данных", _free > 3.0,
                               f"{_free:.1f} ГБ" + (" — маловато для моделей (~4 ГБ)" if _free <= 3.0 else "")))
            except Exception as e:  # noqa: BLE001
                checks.append(("Свободно на диске", True, str(e)))
            for name, ok, detail in checks:
                st.write(("✅ " if ok else "❌ ") + f"**{name}** — {detail}")
            if all(ok for _, ok, _ in checks):
                st.success("Все проверки пройдены — жмите «▶ Индексировать»; если статус "
                           "не сменится, пришлите содержимое журнала выше.")


# ─────────────────────────────── КОНТАКТЫ ───────────────────────────────
def contacts_panel(project: str) -> None:
    from pmoos.ingest.inventory import load_contacts, save_contacts

    data = load_contacts(project)
    st.markdown("**Проектировщики**")
    designers = data.get("designers", [])
    dn = st.text_input("ФИО / организация", key="dn_name")
    dr = st.text_input("Роль (ГИП, ГАП, инженер-эколог…)", key="dn_role")
    de = st.text_input("E-mail / телефон", key="dn_email")
    if st.button("➕ Добавить проектировщика") and dn:
        designers.append({"name": dn, "role": dr, "email": de})
        save_contacts(project, {"designers": designers, "experts": data.get("experts", [])})
        st.rerun()
    for d in designers:
        st.write(f"• {d.get('name','')} — {d.get('role','')} {d.get('email','')}")

    st.markdown("**Эксперты**")
    experts = data.get("experts", [])
    en = st.text_input("ФИО эксперта", key="ex_name")
    eo = st.text_input("Экспертная организация", key="ex_org")
    if st.button("➕ Добавить эксперта") and en:
        experts.append({"name": en, "org": eo})
        save_contacts(project, {"designers": designers, "experts": experts})
        st.rerun()
    for e in experts:
        st.write(f"• {e.get('name','')} — {e.get('org','')}")
