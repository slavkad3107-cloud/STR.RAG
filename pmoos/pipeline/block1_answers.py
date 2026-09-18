"""Блок 1 (МОДУЛЬ 4): найти ответ на каждое замечание ПМООС с указанием источника.

Логика:
  1. Загрузить ВСЕ замечания (ingest.remarks) — не теряем ни одного.
  2. Один раз поднять ресурсы (эмбеддер, BM25-корпус, реранкер) — батчевый
     retrieval вместо N независимых поисков (ускорение для 75 замечаний).
  3. По каждому замечанию найти релевантные фрагменты разделов-источников
     (ТКР/ПОС/ИЭИ/…) с провенансом (раздел/файл/страница).
  4. Сгенерировать ответ ИИ (провайдер/модель — автоматически под модуль),
     параллельными запросами (batch_chat).
  5. Прогнать проверку согласованности (consistency) и каскад (cascade).
  6. Сохранить предложения в answers.json со статусом «proposed» — финальное
     принятие за пользователем (human-in-the-loop).

Ничего из проектных файлов не сохраняется отдельно — работаем по индексу.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import Config, load_config
from ..paths import project_paths
from ..ingest.remarks import load_remarks, Remark
from ..ingest.sections import source_section_codes
from ..retrieval.hybrid import HybridRetriever
from ..core.ai_providers import batch_chat
from ..core.json_utils import extract_json_safe
from .consistency import compare
from ..graph.cascade import explain_cascade, downstream

_SYS_TMPL = (
    "Ты — главный инженер профильного раздела, готовишь ответы на замечания "
    "государственной экспертизы к разделу «{target}» проектной документации "
    "(Постановление Правительства РФ №87). Отвечай профессионально, по существу, "
    "со ссылками на конкретные данные проекта и действующие нормативы. Не "
    "выдумывай данные, которых нет в предоставленных фрагментах: если данных не "
    "хватает — прямо укажи, какой раздел/расчёт нужно дополнить."
)
# обратная совместимость: прежнее имя используется в тестах и внешних вызовах
_SYS = _SYS_TMPL.format(target="Перечень мероприятий по охране окружающей среды (ПМООС)")


_PASSPORT_TOPIC_RX = re.compile(
    r"численн|работающ|работник|рабочих|персонал|протяж[её]н|длин\w*\s+(?:трасс|участк|дорог)|"
    r"продолжительн|срок\w*\s+(?:строит|реконстр|производства)|площад\w*\s+(?:участк|отвод|землеотвод)|"
    r"пусков\w+\s+комплекс", re.I)
_DONE_RX = re.compile(
    r"\b(?:внесен[ыоа]?|уточнен[ыоа]?|пересчитан[ыоа]?|указан[ыоа]?|добавлен[ыоа]?|дополнен[ыоа]?|"
    r"откорректирован[ыоа]?|исправлен[ыоа]?|приведен[ыоа]?|актуализирован[ыоа]?|заменен[ыоа]?|"
    r"выполнен[ыоа]?|представлен[ыоа]?)\b", re.I)


def _sys_for(target: str, scope_text: str | None = None) -> str:
    """scope_text — замечание + найденные фрагменты: справка об устаревших
    нормативах даётся только по документам, которые там названы."""
    from ..ingest.sections import target_name
    base = _SYS_TMPL.format(target=target_name(target))
    try:
        from .volumes import normatives_block
        nb = normatives_block(scope_text=scope_text)
    except Exception:  # noqa: BLE001
        nb = ""
    if nb:
        base += ("\n\nУСТАРЕВШИЕ НОРМАТИВЫ, названные в замечании или в томе (не называть их "
                 "актуальными; в «стало» ставить действующие):\n" + nb +
                 "\nФразы «в актуальной/действующей редакции» без номера и даты документа запрещены."
                 "\nНе ссылайся на нормативный документ, если его нет ни в замечании, ни во фрагментах.")
    return base

_USER_TMPL = (
    "ЗАМЕЧАНИЕ ЭКСПЕРТА №{num}:\n«{remark}»\n\n"
    "{volumes_block}"
    "НАЙДЕННЫЕ ФРАГМЕНТЫ ПРОЕКТНОЙ ДОКУМЕНТАЦИИ (источники; фрагменты с меткой "
    "{{ТОМ-АДРЕСАТ …}} — из самого тома, который правим):\n{context}\n\n"
    "ПРАВИЛА (нарушение = брак):\n"
    "1) edit_was — ТОЛЬКО дословная копия фрагмента с меткой {{ТОМ-АДРЕСАТ}} (без "
    "пересказа и без текста замечания); если такого фрагмента нет — оставь edit_was "
    "пустым и напиши в missing_data, какой пункт тома нужен.\n"
    "2) Реквизиты (номера лицензий/договоров/писем/справок, названия организаций, "
    "даты, марки оборудования) — ТОЛЬКО из фрагментов; иначе плейсхолдер «№ ___ от "
    "___» и документ в attachments.\n"
    "3) Если замечание требует документ/пересчёт, которых нет в фрагментах (договор, "
    "лицензия, справка ЦГМС, СЭЗ, отчёт, согласование, расчёт в УПРЗА/акустике) — "
    "ответ начинается со слов «Требуется:» с перечнем, что запросить/пересчитать; "
    "не пиши «представлен/приложен/выполнен».\n"
    "4) Числа по пусковым комплексам — только из паспорта проекта и фрагментов того "
    "же ПК; числа другого ПК в том не переносить; при РАСХОЖДЕНИИ источников — "
    "назвать оба значения и какое принято.\n"
    "6) Если данных нет («Требуется…»), в edit_shall — только формулировка с "
    "плейсхолдерами «<значение по …>» без придуманных чисел, кодов и организаций.\n"
    "7) edit_shall не должен повторять edit_was без изменений; если «было» найдено, "
    "edit_shall обязателен.\n"
    "5) edit_location — существующий пункт/таблица тома-адресата (как во фрагментах).\n\n"
    "Сформируй ответ строго в формате JSON:\n"
    "{{\n"
    '  "answer": "текст ответа эксперту (что сделано/уточнено в ПМООС)",\n'
    '  "correction": "краткая суть правки одной фразой",\n'
    '  "edit_location": "ТОЧНЫЙ адрес правки: том, раздел, пункт, таблица/рисунок '
    '(например: Том 6.1, п. 2.1.4, Таблица 2.3). Если адрес не следует из '
    'источников — пустая строка",\n'
    '  "edit_was": "как написано СЕЙЧАС — дословная цитата или близкий пересказ '
    'фрагмента, который надо заменить (пусто, если текста ещё нет и его надо '
    'ДОБАВИТЬ)",\n'
    '  "edit_shall": "как должно быть — ГОТОВЫЙ текст для вставки в том, с '
    'конкретными значениями/ссылками, а не описание задачи",\n'
    '  "attachments": ["какие документы приложить/получить: точное название и '
    'кто выдаёт, напр. «Справка Псковского ЦГМС о фоновых концентрациях»"],\n'
    '  "used_sources": [номера фрагментов, реально использованных, напр. [1,3]],\n'
    '  "confidence": "high|medium|low",\n'
    '  "missing_data": "какие ИСХОДНЫЕ ДАННЫЕ отсутствуют в документации '
    '(конкретно: что за данные и в каком томе/разделе их не хватает); пусто, '
    'если всё есть",\n'
    '  "volume_edits": {{"6.1": {{"edit_location": "…", "edit_was": "…", '
    '"edit_shall": "…"}}, "6.2": {{…}}}} — ТОЛЬКО если томов-адресатов несколько: '
    'правка ДЛЯ КАЖДОГО тома со своими числами; иначе пустой объект {{}}\n'
    "}}\n"
    "ВАЖНО: edit_was/edit_shall — это то, что инженер скопирует в том, поэтому "
    "пиши формулировками документа, а не «необходимо уточнить». "
    "Верни ТОЛЬКО JSON."
)


def _center_snippet(text: str, max_chars: int, anchor: str = "") -> str:
    """Обрезка вокруг НАЙДЕННОГО места, а не «первые N символов».

    Находка аудита: обрезка до 900 символов применялась ПОСЛЕ склейки соседних
    чанков и таблиц — в запрос уходило начало соседнего фрагмента, а сама
    находка (например строка «ИТОГО: 12,345 т/год») в контекст не попадала."""
    text = text or ""
    if len(text) <= max_chars:
        return text
    pos = text.find(anchor) if anchor else -1
    if pos < 0:
        pos = 0
    start = max(0, pos - max_chars // 3)
    out = text[start:start + max_chars]
    return ("…" if start else "") + out + ("…" if start + max_chars < len(text) else "")


def _format_context(hits: list[dict], limit: int = 8,
                    max_chars: int = 3000) -> tuple[str, list[dict]]:
    lines, srcs = [], []
    for i, h in enumerate(hits[:limit], 1):
        pl = h.get("payload", {})
        loc = pl.get("loc", "")
        file = pl.get("file", "")
        sec = pl.get("section", "")
        # 3000 символов вместо 900: окно моделей — десятки тысяч токенов, а
        # таблица выбросов со склеенным заголовком в 900 символов не влезала
        snippet = _center_snippet(h.get("text", "") or "", max_chars,
                                  (pl.get("match") or ""))
        tag = pl.get("_tag") or ""
        lines.append(f"[{i}] {('{' + tag + '} ') if tag else ''}(раздел: {sec}; файл: {file}; место: {loc})\n{snippet}")
        srcs.append({"n": i, "file": file, "loc": loc, "section": sec,
                     "score": round(float(h.get("rerank_score", h.get("rrf_score", h.get("score", 0.0)))), 4),
                     "snippet": snippet[:300]})
    return "\n\n".join(lines), srcs


def _process(raw: str) -> dict:
    data = extract_json_safe(raw, expect="object") or {}
    if not isinstance(data, dict):
        data = {}
    return data


# №10-6: категории замечаний для систематизации в М4
CATEGORIES = ["Перерасчёт", "Нормативы", "Доп. документы", "Ввести данные",
              "Правка по источникам"]


def _classify_remark(text: str) -> str:
    """Детерминированная классификация замечания по типу требуемого действия."""
    t = (text or "").lower()

    def has(*ws: str) -> bool:
        return any(w in t for w in ws)

    if has("перерасч", "пересчит", "упрза", "рассеиван", "высот источник", "высоты источник",
           "объем газовоздушн", "объём газовоздушн", "акустическ"):
        return "Перерасчёт"
    if has("расчёт", "расчет") and has("уточн", "выполн", "привести",
                                        "откоррект", "провести", "повтор"):
        return "Перерасчёт"
    # документы — РАНЬШЕ нормативов (проверка 15.09: 31 замечание про договоры/
    # справки/СЭЗ уходило в «Нормативы» из-за слов «в соответствии с требованиями»)
    if has("приложить", "представить", "предоставить", "лиценз", "договор", "справк",
           "письм", "протокол", "паспорт отход", "сертификат", "выписк", "согласован",
           "санитарно-эпидемиологическ", "сэз", "заключени", "отчет по", "отчёт по",
           "актуальн", "неактуальн", "устаревш"):
        return "Доп. документы"
    if has("гост", "санпин", "снип", "гн 2", "сп 2", "сп 5", "норматив", "методик",
           "приказ", "постановлен", "-фз", "фз-", "в соответствии с требованиями"):
        return "Нормативы"
    if has("указать", "заполнить", "внести данные", "привести данные", "добавить данные",
           "отсутствуют данные", "не указан", "не приведен", "не приведён", "не представлены данные"):
        return "Ввести данные"
    return "Правка по источникам"


_CONF_VALUES = {"high", "medium", "low"}
_CONF_MAP = {"высокая": "high", "средняя": "medium", "низкая": "low",
             "high": "high", "medium": "medium", "med": "medium", "low": "low"}


def _normalize_answer(data: dict) -> dict:
    """Валидация/нормализация схемы ответа ИИ (устойчивость к вольностям модели):
    непустые строковые поля, confidence из фиксированного набора, used_sources → int."""
    if not isinstance(data, dict):
        return {"answer": "", "correction": "", "missing_data": "",
                "confidence": "low", "used_sources": []}
    out = dict(data)
    # edit_location/was/shall — структурированная правка «где / было / стало»
    # (жалоба пользователя: «не понятно, что на что менять»)
    for key in ("answer", "correction", "missing_data",
                "edit_location", "edit_was", "edit_shall"):
        v = out.get(key)
        out[key] = v.strip() if isinstance(v, str) else ("" if v is None else str(v))
    att = out.get("attachments")
    if isinstance(att, str):
        att = [x.strip() for x in att.split(";") if x.strip()]
    out["attachments"] = [str(x).strip() for x in att if str(x).strip()] \
        if isinstance(att, list) else []
    c = str(out.get("confidence", "") or "").strip().lower()
    c = _CONF_MAP.get(c, c)
    out["confidence"] = c if c in _CONF_VALUES else ("low" if not out.get("answer") else "medium")
    us = out.get("used_sources")
    norm_us: list[int] = []
    if isinstance(us, list):
        for x in us:
            try:
                norm_us.append(int(x))
            except (TypeError, ValueError):
                try:
                    norm_us.append(int(str(x).strip("[] .")))
                except ValueError:
                    pass
    out["used_sources"] = norm_us
    return out


def _provenance(cfg: Config, object_type: str) -> dict:
    """Снимок конфигурации пайплайна — чтобы при регрессии было видно, ЧЕМ
    отличался прогон (модель/чанкинг/top_k/rerank/expansion/версия).

    Информационный снимок НЕ должен ронять run_block1 (он собирается в самом
    конце, после всех затрат на LLM) — любые касты под защитой."""
    from .. import __version__
    prov: dict = {"version": __version__, "object_type": object_type}
    try:
        prov.update({
            "chunking_mode": str(cfg.get("chunking.mode", "char")),
            "top_k": int(cfg.get("retrieval.top_k", 8)),
            # дефолт = фактическому дефолту поиска (hybrid.py), чтобы снимок не врал
            "candidates": int(cfg.get("retrieval.candidates", 60)),
            "use_rerank": bool(cfg.get("retrieval.use_rerank", True)),
            "reranker_max_length": int(cfg.get("reranker.max_length", 1024)),
            "expansions": (int(cfg.get("retrieval.expansions", 3))
                           if cfg.get("retrieval.use_query_expansion", True) else 0),
            "bm25_weight": float(cfg.get("retrieval.bm25_weight", 1.0)),
            "dense_weight": float(cfg.get("retrieval.dense_weight", 1.0)),
        })
    except Exception:  # noqa: BLE001 — мусор в конфиге не должен убить результат
        prov["config_error"] = "часть значений конфига не удалось прочитать"
    try:  # провайдер/модель — намерение (фактического из-за fallback-цепочки может отличаться)
        prov["answer_provider"] = str(cfg.get("ai.modules.module4.provider",
                                              cfg.get("ai.default_provider", "")) or "")
    except Exception:  # noqa: BLE001
        prov["answer_provider"] = ""
    return prov


def run_block1(project: str, cfg: Config | None = None, *,
               remarks_path: str | Path | None = None,
               object_type: str | None = None,
               progress=None) -> dict[str, Any]:
    cfg = cfg or load_config()
    object_type = object_type or cfg.get("object_type", "площадной")
    paths = project_paths(project)

    # 1) замечания
    if remarks_path is None:
        # ищем файл замечаний: постоянная папка remarks/ — приоритет имени со
        # словом «замечания»; если такого нет — единственный файл папки или
        # новейший (ревью: «⏯ Продолжить» после перезапуска приложения не
        # находил файл с произвольным именем и падал). Затем — старое место.
        cand = []
        rd = paths.get("remarks_dir")
        if rd and rd.exists():
            allf = [fp for fp in sorted(rd.rglob("*")) if fp.is_file()]
            # среди нескольких файлов «замечаний» берём СВЕЖАЙШИЙ, а не первый
            # по алфавиту (ревью: после F5 стартовал старый раунд замечаний)
            kw = sorted((fp for fp in allf
                         if any(k in fp.name.lower() for k in ("замечан", "remark"))),
                        key=lambda f: f.stat().st_mtime, reverse=True)
            cand = kw[:1] or (allf if len(allf) == 1 else
                              sorted(allf, key=lambda f: f.stat().st_mtime,
                                     reverse=True)[:1])
        if not cand and paths["uploads"].exists():
            cand = [fp for fp in sorted(paths["uploads"].rglob("*"))
                    if fp.is_file() and any(k in fp.name.lower()
                                            for k in ("замечан", "remark"))]
        remarks_path = cand[0] if cand else None
    if not remarks_path:
        raise FileNotFoundError("Не найден файл замечаний (ожидается имя со словом «замечания»). "
                                "Загрузите его в поле выше.")
    rp = Path(remarks_path)
    if not rp.exists():
        raise FileNotFoundError(
            f"Файл замечаний не найден на диске: {rp}. Загрузите файл заново в поле выше — "
            f"теперь он сохраняется в постоянную папку remarks/ и не удаляется кнопкой "
            f"«Очистить временные файлы».")
    if rp.suffix.lower() == ".docx":
        with open(rp, "rb") as _fh:
            _head = _fh.read(8)
        if _head[:2] != b"PK":
            if _head == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
                # это старый бинарный .doc, переименованный в .docx —
                # load_remarks сам сконвертирует его через Microsoft Word
                pass
            else:
                raise ValueError(
                    f"Файл «{rp.name}» повреждён или это не настоящий docx (нет ZIP-заголовка). "
                    f"Откройте его в Word и пересохраните, либо загрузите заново.")
    remarks: list[Remark] = load_remarks(rp, cfg)
    if not remarks:
        raise ValueError("Из файла замечаний не удалось извлечь ни одного пункта.")

    # РЕЗЮМ (v0.33): уже отвеченные замечания не переспрашиваем — «Продолжить»
    # после Стопа/обрыва доделывает только остаток. Готовым считаем ответ
    # принятый/правленый ЛИБО с непустым текстом.
    # «Стоп», нажатый ПОКА ИДЁТ подготовка (разбор PDF бывает долгим), раньше
    # терялся: флаг сбрасывался уже ПОСЛЕ разбора (находка аудита). Теперь флаг
    # снимает тот, кто ЗАПУСКАЕТ прогон: фоновый _main — до подготовки, прямой
    # вызов — здесь; а тут мы только проверяем нажатие «во время подготовки».
    if _ANS_BG:
        if _ans_stop_requested(project):
            _ans_progress(project, 0, 0,
                          "⏹ Остановлено до начала генерации.", status="paused")
            return load_answers(project) or {"answers": []}
    else:
        clear_answers_stop(project)

    # ДУБЛИ НОМЕРОВ (ревью): «перезапуск нумерации с 1» из PDF/таблиц давал два
    # замечания №N — by_num схлопывал бы их в один ответ. Перенумеровываем:
    # второй дубль становится «N.2» и живёт своей жизнью.
    _seen: dict[str, int] = {}
    for r in remarks:
        k = str(r.number)
        _seen[k] = _seen.get(k, 0) + 1
        if _seen[k] > 1:
            r.number = f"{k}.{_seen[k]}"

    import re as _re

    def _norm_txt(s: str) -> str:
        return _re.sub(r"\s+", " ", (s or "")).strip().lower()

    prev_by_num = {str(a.get("number")): a
                   for a in (load_answers(project) or {}).get("answers", [])}
    rem_by_num = {str(r.number): r for r in remarks}
    # Готовым считаем ответ, только если ТЕКСТ замечания не изменился (ревью:
    # замена файла замечаний с теми же номерами иначе молча выдавала старые
    # ответы на новые вопросы) и он принят/правлен ЛИБО непустой и не отклонён.
    done_prev = {}
    for n, a in prev_by_num.items():
        r = rem_by_num.get(n)
        if r is not None and _norm_txt(a.get("remark")) not in ("", _norm_txt(r.text)):
            continue  # текст замечания сменился → переспросить
        if a.get("status") in ("accepted", "edited") or (
                a.get("status") != "rejected"           # ✗ отклонён → переспросить
                and not a.get("needs_ai")               # заглушка «ИИ не ответил» → переспросить
                and (a.get("user_answer") or a.get("answer") or "").strip()):
            done_prev[n] = a
    pending = [r for r in remarks if str(r.number) not in done_prev]
    by_num: dict[str, dict] = {n: a for n, a in done_prev.items()}
    total = len(remarks)
    _ans_progress(project, total, len(by_num),
                  (f"Возобновление: готово {len(by_num)}/{total}…" if by_num
                   else f"Старт: замечаний {total}…"))

    # 2) обработка ПАКЕТАМИ (v0.33): между пакетами — сохранение на диск,
    #    прогресс и проверка «Стоп». Обрыв/стоп теряет максимум один пакет.
    pack_size = max(1, int(cfg.get("answers.batch_size", 10)))
    # ЦЕЛЕВОЙ РАЗДЕЛ (ТЗ 30.07): ООС / ИЭИ / ОЦЕНКА — от него зависят и набор
    # разделов-источников, и формулировки промпта, и «том раздела» у ответа
    target = str(cfg.get("target_section", "OOS") or "OOS")
    src_codes = source_section_codes(object_type, target)
    for p0 in range(0, len(pending), pack_size):
        if _ans_stop_requested(project):
            _save_merged(project, remarks, by_num, cfg, object_type, partial=True)
            _ans_progress(project, total, len(by_num),
                          f"⏹ Остановлено: готово {len(by_num)}/{total}. "
                          f"«⏯ Продолжить» доделает остальные.", status="paused")
            return load_answers(project)
        pack = pending[p0:p0 + pack_size]
        _ans_progress(project, total, len(by_num),
                      f"Пакет {p0 // pack_size + 1}: замечания "
                      f"{pack[0].number}–{pack[-1].number}…")
        pack_ans = _answer_pack(project, cfg, object_type, pack, src_codes, progress)
        for a in pack_ans:
            by_num[str(a["number"])] = a
        _save_merged(project, remarks, by_num, cfg, object_type, partial=True)
        # ИИ МЁРТВ ЦЕЛИКОМ (все провайдеры упали на всём пакете) — дальше идти
        # бессмысленно: 06.09.2026 так «успешно» получились 39 пустых ответов
        # из 75. Останавливаемся с понятной причиной; готовое сохранено.
        if pack_ans and all(a.get("needs_ai") for a in pack_ans):
            err = next((a.get("error") for a in pack_ans if a.get("error")), "") \
                or "провайдер не вернул ответ"
            _ans_progress(project, total, len(by_num),
                          f"⛔ ИИ недоступен — остановлено после пакета "
                          f"{p0 // pack_size + 1}: {str(err)[:220]}. Откройте СЕРВИС, "
                          f"выберите рабочую модель и снова нажмите «① Найти ответы» — "
                          f"ответы без ИИ переспросятся.", status="error")
            return load_answers(project)
        _ans_progress(project, total, len(by_num),
                      f"Готово ответов: {len(by_num)}/{total}")

    out = _save_merged(project, remarks, by_num, cfg, object_type, partial=False)
    # v0.55: если тома уже загружены во ВЫГРУЗКУ — проверить «было» по их полному тексту
    try:
        from .volumes import reverify_answers
        rv = reverify_answers(project)
        if rv.get("verified"):
            print(f"[block1] «было» подтверждено по полному тексту томов: {rv['verified']} из {rv['checked']}",
                  flush=True)
            out = load_answers(project)
    except Exception as e:  # noqa: BLE001
        print(f"[block1] повторная проверка «было»: {e}", flush=True)
    _ans_progress(project, total, len(by_num),
                  f"Готово: {len(by_num)} ответов.", status="done")
    return out


def _answer_pack(project: str, cfg: Config, object_type: str, remarks: list,
                 src_codes: list, progress=None) -> list[dict]:
    """Обработать ПАКЕТ замечаний: retrieval → задания ИИ → batch → сборка.
    Тело прежнего монолитного run_block1, вынесенное для пакетного режима."""
    retr = HybridRetriever(cfg)
    try:
        queries = [r.text for r in remarks]
        if progress:
            progress(0, len(remarks), "Поиск источников по замечаниям…")
        # ПОИСК ПО ВСЕЙ БАЗЕ (07.09.2026, замечание юзера: «план был — RAG по
        # ВСЕМ разделам ПД, а потом искать ответы»). Раньше фильтр src_codes
        # оставлял только разделы с флагом is_source: на ОПОЧКЕ невидимыми были
        # 14 520 из 44 067 фрагментов (ППО, ТКР-как-АР, ОДИ, ПБ, ИОС.СС) — треть
        # базы. Ограничить набор можно только осознанно: answers.search_all_sections: false.
        all_sections = bool(cfg.get("answers.search_all_sections", True))
        hits_per = retr.batch_search(project, queries,
                                     sections=(None if all_sections else (src_codes or None)),
                                     top=int(cfg.get("retrieval.top_k", 8)))
        # №10-5: к какому ТОМУ ООС относится замечание — лёгкий поиск top-1
        # только по разделу OOS (томов может быть несколько). Расширение запроса
        # не нужно; пул реранка сжат 60→16: этот пасс выбирает ЛИШЬ файл-том
        # (payload.file) для top-1, полный пул тратил ~половину rerank-бюджета
        # всего прогона на второстепенное поле.
        try:
            _target = str(cfg.get("target_section", "OOS") or "OOS")
            oos_per = retr.batch_search(
                project, queries, sections=[_target], top=1, use_expansion=False,
                candidates=int(cfg.get("retrieval.oos_candidates", 16)))
        except Exception:  # noqa: BLE001
            oos_per = []
    finally:
        # освобождаем embedded-Qdrant СРАЗУ: он однопроцессный, и удержание
        # блокировки ломало фоновую индексацию («already accessed»)
        retr.close()
    oos_by_num: dict[str, str] = {}
    for _k, _r in enumerate(remarks):
        _hit = (oos_per[_k][0] if _k < len(oos_per) and oos_per[_k] else None)
        oos_by_num[str(_r.number)] = ((_hit.get("payload") or {}).get("file", "")
                                      if _hit else "")
    # ТОМ-АДРЕСАТ И ПУСКОВОЙ КОМПЛЕКС (v0.55, проверка на ОПОЧКЕ): том берётся из
    # текста замечания (не из первого чанка), фрагменты чужого ПК — вон/в конец,
    # плюс отдельный поиск ПО САМОМУ ТОМУ для дословного «было».
    from .volumes import (oos_volumes, target_volumes, pk_of, pk_filter,
                          passport_text, volume_token)
    oos_map = oos_volumes(project, str(cfg.get("target_section", "OOS") or "OOS"))
    oos_files = set(oos_map.values())
    tv_by_idx: dict[int, list[str]] = {}
    vol_hits_by_idx: dict[int, dict[str, list[dict]]] = {}
    if oos_map:
        for _k, _r in enumerate(remarks):
            tv = target_volumes(_r.text, oos_map)
            tv_by_idx[_k] = tv
            if len(tv) == 1:
                oos_by_num[str(_r.number)] = oos_map[tv[0]]
            pk = pk_of(tv[0]) if len(tv) == 1 else None
            hits_per[_k] = pk_filter(hits_per[_k], pk, oos_files)
        # фрагменты тома-адресата: по одному запросу на (замечание, том)
        per_file: dict[str, list[int]] = {}
        for _k, tv in tv_by_idx.items():
            for tok in tv[:3]:
                per_file.setdefault(oos_map[tok], []).append(_k)
        retr2 = HybridRetriever(cfg)
        try:
            for fname, idxs in per_file.items():
                tok = next(t for t, f in oos_map.items() if f == fname)
                try:
                    res = retr2.batch_search(project, [remarks[i].text for i in idxs],
                                             files=[fname], top=3, candidates=24,
                                             use_expansion=False)
                except Exception as e:  # noqa: BLE001
                    print(f"[block1] поиск по тому {tok}: {e}", flush=True)
                    res = [[] for _ in idxs]
                for i, hs in zip(idxs, res):
                    for h in hs:
                        h["payload"] = dict(h.get("payload") or {}, _tag=f"ТОМ-АДРЕСАТ {tok}")
                    vol_hits_by_idx.setdefault(i, {})[tok] = hs
        finally:
            retr2.close()
        # том-адресат первым в контексте; дубли по id убираем
        for _k in range(len(remarks)):
            vh = [h for hs in (vol_hits_by_idx.get(_k) or {}).values() for h in hs]
            if vh:
                seen_ids = {str(h.get("id")) for h in vh}
                hits_per[_k] = vh + [h for h in hits_per[_k] if str(h.get("id")) not in seen_ids]
    passport = passport_text(project, oos_map)
    # ОБЯЗАТЕЛЬНЫЕ ИСТОЧНИКИ ИЗ «ОСНОВАНИЯ» (тестировщик №2, рек. 3): том,
    # названный в замечании («Раздел 3, Том 3.1.2, лист ТКР.АД-ВР19»), ищется
    # прицельно и попадает в контекст с меткой {ОСНОВАНИЕ …}
    from .volumes import ref_volumes, _state_files
    _all_files = {}
    for _rel in _state_files(project):
        _tok = volume_token(_rel)
        if _tok and _tok not in oos_map:
            _all_files.setdefault(_tok, _rel)
    ref_by_idx: dict[int, list[str]] = {}
    for _k, _r in enumerate(remarks):
        ref_by_idx[_k] = [t for t in ref_volumes(_r.text, oos_map) if t in _all_files][:3]
    per_ref: dict[str, list[int]] = {}
    for _k, toks in ref_by_idx.items():
        for tok in toks:
            per_ref.setdefault(_all_files[tok], []).append(_k)
    if per_ref:
        retr3 = HybridRetriever(cfg)
        try:
            for fname, idxs in per_ref.items():
                tok = next(t for t, f in _all_files.items() if f == fname)
                try:
                    res = retr3.batch_search(project, [remarks[i].text for i in idxs],
                                             files=[fname], top=2, candidates=16, use_expansion=False)
                except Exception as e:  # noqa: BLE001
                    print(f"[block1] поиск по основанию {tok}: {e}", flush=True)
                    res = [[] for _ in idxs]
                for i, hs in zip(idxs, res):
                    for h in hs:
                        h["payload"] = dict(h.get("payload") or {}, _tag=f"ОСНОВАНИЕ том {tok}")
                    seen_ids = {str(h.get("id")) for h in hits_per[i]}
                    add = [h for h in hs if str(h.get("id")) not in seen_ids]
                    # после фрагментов тома-адресата, перед общим поиском
                    nvol = sum(1 for h in hits_per[i] if (h.get("payload") or {}).get("_tag", "").startswith("ТОМ-АДРЕСАТ"))
                    hits_per[i] = hits_per[i][:nvol] + add + hits_per[i][nvol:]
        finally:
            retr3.close()

    # 3) формируем задания для ИИ (с few-shot из памяти прошлых проектов)
    use_mem = bool(cfg.get("memory.enabled", True))
    mem_k = int(cfg.get("memory.k", 2))
    jobs, ctx_sources = [], []
    for r, hits in zip(remarks, hits_per):
        ctx, srcs = _format_context(hits, limit=int(cfg.get("retrieval.top_k", 8)),
                                    max_chars=int(cfg.get("retrieval.snippet_chars", 3000)))
        ctx_sources.append((srcs, hits))
        _tv = tv_by_idx.get(len(jobs), [])
        _vb = ""
        if _tv:
            _vb = ("ТОМА-АДРЕСАТЫ ПРАВКИ: " + ", ".join(f"том {t} ({oos_map[t]})" for t in _tv)
                   + (" — правка нужна В КАЖДОМ томе (заполни volume_edits)" if len(_tv) > 1 else "")
                   + "\n")
        if passport and _PASSPORT_TOPIC_RX.search(r.text or ""):
            # справка нужна только замечаниям про численность/длину/сроки/площадь и
            # только по ПК своего тома (тестировщик №4: «6,35 км» разошлось по 16
            # ответам, в тексте появился несуществующий «Паспорт проекта»)
            _pl = [ln for ln in passport.splitlines()
                   if not _tv or any(f"(том {t})" in ln for t in _tv)]
            if _pl:
                _vb += ("СЛУЖЕБНАЯ СПРАВКА ПО ПУСКОВЫМ КОМПЛЕКСАМ (значение · источник; в тексте ответа "
                        "ссылайся на сам источник, слов «паспорт проекта» и «справка» не пиши):\n"
                        + "\n".join(_pl) + "\n")
        if _vb:
            _vb += "\n"
        user_msg = _USER_TMPL.format(num=r.number, remark=r.text, context=ctx or "(не найдено)",
                                     volumes_block=_vb)
        if use_mem:
            try:
                from ..memory import fewshot_block
                fs = fewshot_block(r.text, k=mem_k, exclude_project=project, cfg=cfg)
            except Exception:  # noqa: BLE001
                fs = ""
            if fs:
                user_msg = fs + "\n\n" + user_msg
        jobs.append([
            {"role": "system",
             "content": _sys_for(str(cfg.get("target_section", "OOS") or "OOS"),
                                 scope_text=(r.text or "") + "\n" + (ctx or ""))},
            {"role": "user", "content": user_msg},
        ])

    if progress:
        progress(0, len(remarks), "Генерация ответов ИИ (параллельно)…")
    results = batch_chat(cfg, jobs, processor=_process, module="module4",
                         role="answer", json_mode=True)

    # JSON-повтор в ПАКЕТНОМ пути (v0.21): если вызов прошёл (ok), но JSON не
    # распарсился (пустой result) — один batch-повтор с жёсткой инструкцией,
    # без кэша. Раньше такой повтор был только в одиночном chat_json, и битый
    # JSON в батче давал пустой ответ на замечание.
    if cfg.get("ai.json_repair_retry", True):
        bad = [i for i, res in enumerate(results)
               if res.get("ok") and not res.get("result")]
        if bad:
            if progress:
                progress(0, len(bad), f"Повтор JSON для {len(bad)} ответов…")
            retry_jobs = []
            for i in bad:
                retry_jobs.append(list(jobs[i]) + [{
                    "role": "user",
                    "content": ("Твой предыдущий ответ не распарсился как JSON-объект. "
                                "Верни ТОЛЬКО JSON-объект по требуемой схеме — без "
                                "markdown, без ```-ограждений и без пояснений."),
                }])
            retry_res = batch_chat(cfg, retry_jobs, processor=_process,
                                   module="module4", role="answer",
                                   json_mode=True, use_cache=False)
            for i, rr in zip(bad, retry_res):
                if rr.get("ok") and rr.get("result"):
                    results[i] = rr

    # 4) сборка ответов + consistency + cascade
    answers = []
    for idx, (r, (srcs, hits)) in enumerate(zip(remarks, ctx_sources)):
        res = results[idx]
        data = res.get("result") if res.get("ok") else {}
        data = _normalize_answer(data or {})
        used = data.get("used_sources") or []
        matched = [s for s in srcs if s["n"] in set(used)]
        # НЕ подменяем провенанс: если ИИ не указал использованные фрагменты —
        # оставляем список пустым и помечаем флагом (раньше молча клеили srcs[:3]
        # как «источники» — ложная атрибуция, недопустимая для экспертизы).
        used_sources = matched
        sources_unverified = not matched

        answer_text = data.get("answer", "").strip()
        # КОНТРАКТ «БЫЛО» (v0.55): цитата обязана встречаться в томе-адресате;
        # ИИ переставил слова — подставляем дословный фрагмент; нет — «место не
        # подтверждено» (такой ответ не станет заменой в томе)
        from .volumes import verify_was, unsupported_requisites
        _vh_all = vol_hits_by_idx.get(idx) or {}
        _pool = [h for hs in _vh_all.values() for h in hs] or [
            h for h in hits if (h.get("payload") or {}).get("file") in oos_files] or hits
        was_verified, was_score = False, 0.0
        if (data.get("edit_was") or "").strip():
            vw = verify_was(data["edit_was"], _pool)
            was_verified, was_score = vw["verified"], vw["score"]
            if vw["verified"] and vw["quote"]:
                data["edit_was"] = vw["quote"]
            elif not vw["verified"]:
                data["edit_was_unverified"] = data["edit_was"]
                data["edit_was"] = ""
        vol_edits = data.get("volume_edits") if isinstance(data.get("volume_edits"), dict) else {}
        clean_vol_edits: dict[str, dict] = {}
        for _tok, _ed in vol_edits.items():
            if not isinstance(_ed, dict):
                continue
            _e = {k: str(_ed.get(k) or "").strip() for k in ("edit_location", "edit_was", "edit_shall")}
            if _e["edit_was"]:
                _vw = verify_was(_e["edit_was"], _vh_all.get(str(_tok)) or _pool)
                if _vw["verified"] and _vw["quote"]:
                    _e["edit_was"] = _vw["quote"]
                    _e["was_verified"] = True
                else:
                    _e["edit_was_unverified"] = _e["edit_was"]
                    _e["edit_was"] = ""
                    _e["was_verified"] = False
            clean_vol_edits[str(_tok)] = _e
        # общее замечание: верхние поля правки пустые, а правки по томам есть —
        # берём первый том как «лицо» ответа (экспорт таблиц, старые сценарии)
        if clean_vol_edits and not (data.get("edit_shall") or "").strip():
            _first = next(iter(clean_vol_edits.values()))
            data["edit_shall"] = _first.get("edit_shall", "")
            data["edit_location"] = data.get("edit_location") or _first.get("edit_location", "")
            if not (data.get("edit_was") or "").strip() and _first.get("edit_was"):
                data["edit_was"] = _first["edit_was"]
                was_verified = bool(_first.get("was_verified"))
        # «где править» указывает на ЧУЖОЙ том (ПОС/ТКР вместо тома ООС) —
        # правка не может лечь в том-адресат (проверка 15.09: «Том 5.1.3, п. 24»)
        from ..output.docx_writer import _volume_tokens as _vt
        _loc_vols = _vt(data.get("edit_location") or "")
        _tv_set = set(tv_by_idx.get(idx, []))
        location_mismatch = bool(_loc_vols and _tv_set and not (_loc_vols & _tv_set))
        _ctx_full = "\n".join((h.get("text") or "") for h in hits) + "\n" + passport
        unsupported = unsupported_requisites(
            " ".join([answer_text, data.get("edit_shall", ""), data.get("correction", "")]), _ctx_full)
        # ЧИСЛА В «СТАЛО» БЕЗ ИСТОЧНИКА и режим «нет данных» (тестировщик №2, рек. 1–2):
        # если ответ говорит «Требуется…»/не хватает данных, а в «стало» стоят
        # числа, которых нет ни во фрагментах, ни в паспорте, — это сочинённые
        # значения: правка не вносится автоматически (shall_unverified)
        from .volumes import unsupported_numbers, location_from_remark, normalize as _nz
        requires_docs = bool((data.get("missing_data") or "").strip()) or \
            answer_text.lstrip().lower().startswith("требуется") or bool(data.get("attachments"))
        unsupported_nums = unsupported_numbers(data.get("edit_shall", ""), _ctx_full + "\n" + (r.text or ""))
        shall_unverified = bool(unsupported_nums) and requires_docs
        # флаги качества правки
        _shall = (data.get("edit_shall") or "").strip()
        shall_missing = bool(was_verified) and not _shall
        from ..output.docx_writer import _same_text
        no_change = bool(_shall) and bool((data.get("edit_was") or "").strip()) and \
            (_nz(_shall) == _nz(data.get("edit_was") or "") or _same_text(_shall, data.get("edit_was") or ""))
        # ОТВЕТ ЗАЯВЛЯЕТ «ВНЕСЕНО», А ПРАВКА НЕ ЗАВЕРШЕНА (тестировщик №4, рек. 13):
        # заглушки/поля в «стало», непустое «не хватает данных» или числа без источника
        _placeholders = bool(re.search(r"<[^<>\n]{2,80}>|_{3,}|\bХХ+\b|\bXX+\b", _shall))
        answer_overclaims = bool(answer_text) and bool(_DONE_RX.search(answer_text.replace("ё", "е"))) and (
            bool((data.get("missing_data") or "").strip()) or _placeholders or shall_unverified or no_change)
        if answer_overclaims:
            _need = (data.get("missing_data") or "").strip() or (
                "«стало» совпадает с «было»" if no_change else "заполнить отмеченные поля/подтвердить числа")
            answer_text = (f"ПРАВКА НЕ ЗАВЕРШЕНА — требуется: {_need[:400]}. После получения данных правка "
                           f"вносится в: {(data.get('edit_location') or 'место уточнить')[:160]}.\n"
                           f"Черновик ответа: {answer_text}")
        # адрес правки: из подтверждённого «было» (лист) или из текста замечания
        if _shall and not (data.get("edit_location") or "").strip():
            _from_remark = location_from_remark(r.text)
            _tv0 = (tv_by_idx.get(idx) or [""])[0]
            data["edit_location"] = (f"Том {_tv0}, " if _tv0 else "") + (_from_remark or "место уточнить")
        if was_verified:
            _src = _locate_in_hits(data.get("edit_was", ""), hits) or {}
            if _src.get("loc") and _src["loc"] not in (data.get("edit_location") or ""):
                data["edit_location"] = (data.get("edit_location") or "").rstrip(" ;") + f" ({_src['loc']})"
        # источник для consistency = ТЕ ЖЕ фрагменты, что ушли модели в контекст
        # (раньше hits[:5] при контексте top_k=8 — сущности из фрагментов 6-8 давали
        # ложные «unsupported_refs»). Плюс текст самого замечания: норматив,
        # процитированный экспертом, — легитимная ссылка, а не «выдумка» ответа.
        _ctx_limit = int(cfg.get("retrieval.top_k", 8))
        src_text = "\n".join((h.get("text") or "") for h in hits[:_ctx_limit])
        cons = compare(src_text + "\n" + (r.text or ""),
                       answer_text + " " + data.get("correction", ""))

        # СЛАБАЯ ОПОРА НА ИСТОЧНИКИ (по итогам dex-ревью): для замечаний экспертизы
        # ответ «от себя» недопустим. Если retrieval НИЧЕГО не нашёл в ПД — помечаем
        # ответ, чтобы инженер проверил вручную (галлюцинация вероятна).
        low_support = not hits
        # ГЕЙТ ДОСТОВЕРНОСТИ: выдуманные нормативы/ЗВ/техника (consistency.issues)
        # или отсутствие опоры → принудительно снижаем confidence и помечаем.
        unsupported_refs = bool(cons.get("issues"))
        # КАЛИБРОВКА ПО ФАКТАМ (тестировщик №2, рек. 11): high — только дословное
        # «было» + непустое «стало» + ни одного неподтверждённого реквизита/числа +
        # есть источники; «Требуется»/нет источников → не выше medium; выдумки → low
        if unsupported_refs or low_support or unsupported or shall_unverified or no_change \
                or answer_overclaims or not _shall:
            confidence = "low"
        elif was_verified and _shall and used_sources and not requires_docs and not location_mismatch \
                and not no_change and not unsupported_nums:
            confidence = "high"
        else:
            confidence = "medium"

        # каскад: какие разделы затронет правка (по разделам источников; если ИИ не
        # атрибутировал — по найденным поиском, каскад носит справочный характер)
        affected_codes = sorted({s["section"] for s in (used_sources or srcs) if s.get("section")})
        cascade = downstream(project, affected_codes) if affected_codes else {"changed": [], "affected": []}

        # №10-6: категория из файла замечаний (если была колонка), иначе —
        # автоматическая классификация по тексту замечания
        category = (getattr(r, "category", "") or _classify_remark(r.text))
        # ПУСТОГО ОТВЕТА НЕ БЫВАЕТ (замечание юзера 06.09.2026: «(пусто) — так
        # быть не должно ни по одному замечанию: указывать, что данных нет, или
        # предложить варианты»). Если ИИ не ответил — заглушка с причиной,
        # найденными материалами и вариантами ответа; needs_ai=True — при
        # следующем запуске «Найти ответы» такое замечание переспрашивается.
        needs_ai = not answer_text
        if needs_ai:
            answer_text = _stub_answer(category, srcs, res.get("error"))
        answers.append({
            "number": r.number,
            "remark": r.text,
            "oos_volume": oos_by_num.get(str(r.number), ""),  # №10-5
            "category": category,
            "answer": answer_text,
            "needs_ai": needs_ai,
            "correction": data.get("correction", ""),
            # структурированная правка: где / было / стало / что приложить
            "edit_location": data.get("edit_location", ""),
            "edit_was": data.get("edit_was", ""),
            "edit_shall": data.get("edit_shall", ""),
            # ГДЕ НАЙДЕНО «БЫЛО» (ТЗ 08.09: «как было — том, страница, текст
            # страницы»): фрагмент контекста, в котором цитата действительно есть
            "edit_was_src": _locate_in_hits(data.get("edit_was", ""), hits) or {},
            "edit_was_unverified": data.get("edit_was_unverified", ""),
            "was_verified": was_verified,
            "was_score": was_score,
            "target_volumes": tv_by_idx.get(idx, []),
            "volume_edits": clean_vol_edits,
            "unsupported_requisites": unsupported,
            "location_mismatch": location_mismatch,
            "unsupported_numbers": unsupported_nums,
            "shall_unverified": shall_unverified,
            "shall_missing": shall_missing,
            "no_change": no_change,
            "answer_overclaims": answer_overclaims,
            "requires_docs": requires_docs,
            "attachments": data.get("attachments", []),
            "confidence": confidence,
            "missing_data": data.get("missing_data", ""),
            "sources": used_sources,
            "retrieved_sources": srcs,          # что реально нашёл поиск (прозрачность)
            "sources_unverified": sources_unverified,
            "unsupported_refs": unsupported_refs,
            "low_support": low_support,
            "consistency": cons,
            "cascade": cascade,
            "cascade_text": (explain_cascade(project, affected_codes, res=cascade)
                             if affected_codes else ""),
            "status": "proposed",          # proposed|accepted|rejected|edited
            "user_answer": None,
            "error": res.get("error"),
        })
        if progress:
            progress(idx + 1, len(remarks), f"Замечание {r.number}")

    return answers
    # Примечание: прежний kept-merge «не затирать принятые» теперь живёт выше —
    # в резюм-логике run_block1 (готовые ответы вообще не переспрашиваются).


_STUB_VARIANTS = {
    "Ввести данные": [
        "внести запрошенные данные в указанный пункт/таблицу тома по данным "
        "раздела-источника (ПОС, ПЗ, ИОС, изыскания) — см. материалы выше;",
        "если данных в ПД нет — запросить у заказчика / смежного раздела, до "
        "получения пометить место «◈ ВНЕСТИ» и отразить в ведомости недостающих данных.",
    ],
    "Доп. документы": [
        "приложить запрошенный документ (протокол, договор, справку, лицензию) и "
        "сослаться на него в томе;",
        "если документа нет — запросить у заказчика, указать срок представления;",
        "если документ не требуется — дать мотивированное пояснение со ссылкой на НПА.",
    ],
    "Нормативы": [
        "привести формулировку / расчёт в соответствие с указанным нормативом "
        "(проверить действующую редакцию);",
        "если норматив утратил силу — сослаться на действующий документ-замену.",
    ],
    "Правка по источникам": [
        "исправить указанное место тома по данным материалов выше;",
        "при расхождении данных между разделами — согласовать с разделом-источником "
        "(ПОС / ИОС / ПЗ) и внести единое значение во все тома;",
        "если замечание не подтверждается — дать мотивированное пояснение со ссылкой "
        "на лист тома.",
    ],
}


def _stub_answer(category: str, srcs: list[dict], error: str | None) -> str:
    """Текст вместо пустого ответа: причина, что найдено в базе, варианты."""
    reason = "ИИ не вернул ответ"
    if error:
        reason += f" ({str(error)[:220]})"
    L = [f"⚠ АВТОМАТИЧЕСКИЙ ОТВЕТ НЕ СФОРМИРОВАН: {reason}."]
    if srcs:
        L.append("По базе проекта найдены материалы по теме замечания:")
        L += [f"  — {s.get('file', '')} {s.get('loc', '')}".rstrip() for s in srcs[:5]]
    else:
        L.append("Данных по теме замечания в базе проекта НЕ НАЙДЕНО.")
    L.append("Варианты ответа (выберите и отредактируйте):")
    for i, v in enumerate(_STUB_VARIANTS.get(category) or _STUB_VARIANTS["Правка по источникам"],
                          start=1):
        L.append(f"  {i}) {v}")
    L.append("Повторить запрос к ИИ: вкладка СЕРВИС → выбрать рабочую модель → "
             "ОТВЕТЫ → «① Найти ответы» (такие ответы переспрашиваются).")
    return "\n".join(L)


def _save_merged(project: str, remarks: list, by_num: dict[str, dict],
                 cfg: Config, object_type: str, *, partial: bool) -> dict:
    """Собрать answers.json из готовых ответов (в порядке файла замечаний) и
    сохранить. partial=True — промежуточное сохранение между пакетами."""
    # РЕШЕНИЯ ПОЛЬЗОВАТЕЛЯ ПОБЕЖДАЮТ (ревью, high): пока фон генерирует,
    # пользователь мог принять/править/отклонить ответ в интерфейсе — версия
    # С ДИСКА главнее нашей в памяти, иначе очередное пакетное сохранение
    # молча откатывало бы его решение в «proposed».
    for n, a in {str(x.get("number")): x
                 for x in (load_answers(project) or {}).get("answers", [])}.items():
        # ТОЛЬКО принятые/правленые: «отклонён» означает «переспроси заново», и
        # возврат старого отклонённого ответа затирал бы свежесгенерированный
        # (находка аудита, critical). Если нового ответа ещё нет — старый
        # отклонённый остаётся как есть.
        if a.get("status") in ("accepted", "edited") or (
                a.get("status") == "rejected" and n not in by_num):
            by_num[n] = a
    ordered = [by_num[str(r.number)] for r in remarks if str(r.number) in by_num]
    known = {str(r.number) for r in remarks}
    # ответы прошлых прогонов на замечания, которых нет в текущем файле, — не
    # теряем (файл замечаний могли заменить), складываем в конец
    ordered += [a for n, a in by_num.items() if n not in known]
    out = {
        "project": project, "object_type": object_type,
        "block": 1, "count": len(ordered),
        "partial": bool(partial),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "provenance": _provenance(cfg, object_type),   # снимок пайплайна (атрибуция регрессий)
        "answers": ordered,
    }
    _save(project, out)
    return out


def _save(project: str, data: dict) -> Path:
    import os
    p = project_paths(project)["answers"]
    p.parent.mkdir(parents=True, exist_ok=True)
    # атомарно (tmp+replace, tmp уникален для процесса, замена с повторами):
    # «Стоп»/чтение из GUI не должны оставить answers.json битым (ревью)
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _replace_atomic(tmp, p)
    return p


def reset_answers(project: str) -> None:
    """№10-4: полный сброс предложенных ответов (кнопка «🗑 Сбросить» в М4).
    Файл answers.json очищается; журнал решений decisions.jsonl сохраняется."""
    paths = project_paths(project)
    paths["answers"].parent.mkdir(parents=True, exist_ok=True)
    paths["answers"].write_text(
        json.dumps({"answers": [], "reset_at": datetime.now().isoformat()},
                   ensure_ascii=False, indent=1), encoding="utf-8")


def load_answers(project: str) -> dict[str, Any]:
    p = project_paths(project)["answers"]
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _audit_entry(data: dict, number: str, status: str,
                 user_answer: str | None) -> dict:
    """Строка append-only аудита decisions.jsonl со снимком принятого ответа."""
    ans_obj = next((a for a in data.get("answers", [])
                    if str(a["number"]) == str(number)), None)
    entry = {"ts": datetime.now().isoformat(), "number": number,
             "status": status, "user_answer": user_answer}
    if ans_obj is not None:
        from .. import __version__
        entry["snapshot"] = {
            "remark": ans_obj.get("remark", ""),
            "final": (ans_obj.get("user_answer") or ans_obj.get("answer") or "").strip(),
            "correction": ans_obj.get("correction", ""),
            "sources": ans_obj.get("sources", []),
            "confidence": ans_obj.get("confidence", ""),
            "low_support": bool(ans_obj.get("low_support")),
            "unsupported_refs": bool(ans_obj.get("unsupported_refs")),
            "generated_at": data.get("generated_at", ""),
            "version": __version__,
        }
    return entry


def _memorize(project: str, data: dict, number: str) -> None:
    """Пополнить память экспертизы принятым/правленым ответом (best-effort)."""
    try:
        from ..memory import record_one
        ans_obj = next((a for a in data.get("answers", [])
                        if str(a["number"]) == str(number)), None)
        if ans_obj:
            final = (ans_obj.get("user_answer") or ans_obj.get("answer") or "").strip()
            # sources может быть пуст (v0.26: ложный srcs[:3] убран) — раздел
            # берём из найденного поиском, иначе память few-shot потеряет секцию
            sec = ((ans_obj.get("sources") or ans_obj.get("retrieved_sources")
                    or [{}])[0]).get("section", "")
            record_one(remark=ans_obj.get("remark", ""), answer=final,
                       correction=ans_obj.get("correction", ""), section=sec,
                       project=project, number=number)
    except Exception:  # noqa: BLE001
        pass


def _locate_in_hits(text: str, hits: list[dict], limit: int = 8) -> dict | None:
    """В каком фрагменте контекста встречается цитата «было»: файл, место,
    выдержка страницы вокруг найденного. Точное вхождение — 1.0; иначе доля
    совпавших значимых слов (порог 0.5)."""
    import re as _re
    t = _re.sub(r"\s+", " ", (text or "")).strip().lower()
    if len(t) < 12:
        return None
    words = set(_re.findall(r"[а-яёa-z0-9]{4,}", t))
    best, best_score, best_pos = None, 0.0, 0
    for h in hits[:limit]:
        ht = _re.sub(r"\s+", " ", (h.get("text") or ""))
        htl = ht.lower()
        pos = htl.find(t[:80])
        if pos >= 0:
            score = 1.0
        else:
            hw = set(_re.findall(r"[а-яёa-z0-9]{4,}", htl))
            score = len(words & hw) / max(1, len(words))
            first = next((w for w in _re.findall(r"[а-яёa-z0-9]{4,}", t) if w in hw), "")
            pos = htl.find(first) if first else 0
        if score > best_score:
            best, best_score, best_pos = h, score, max(0, pos)
    if not best or best_score < 0.5:
        return None
    pl = best.get("payload") or {}
    ht = _re.sub(r"\s+", " ", (best.get("text") or ""))
    lo = max(0, best_pos - 160)
    return {"file": pl.get("file", ""), "loc": pl.get("loc", ""),
            "section": pl.get("section", ""), "score": round(best_score, 2),
            "snippet": ht[lo:lo + 600]}


_EDIT_FIELDS = ("answer", "correction", "edit_location", "edit_was", "edit_shall",
                "missing_data")


def edit_answer(project: str, number: str, fields: dict) -> dict:
    """РУЧНАЯ ПРАВКА ответа (ТЗ 08.09: таблица с возможностью редактирования):
    ответ / где / было / стало / приложить / не хватает. Статус → edited,
    user_answer = ответ; заглушка «без ИИ» снимается. След — в decisions.jsonl."""
    import re as _re
    data = load_answers(project)
    by_num = {str(a.get("number")): a for a in data.get("answers", [])}
    a = by_num.get(str(number))
    if a is None:
        raise KeyError(f"замечание №{number} не найдено")
    for k in _EDIT_FIELDS:
        if k in fields:
            a[k] = str(fields.get(k) or "").strip()
    if "attachments" in fields:
        att = fields.get("attachments") or []
        if isinstance(att, str):
            att = [x.strip() for x in _re.split(r"[;\n]", att) if x.strip()]
        a["attachments"] = [str(x) for x in att]
    a["status"] = "edited"
    a["user_answer"] = a.get("answer", "")
    a["needs_ai"] = False
    a["edited_at"] = datetime.now().isoformat(timespec="seconds")
    _save(project, data)
    dec = project_paths(project)["decisions"]
    with dec.open("a", encoding="utf-8") as f:
        f.write(json.dumps(_audit_entry(data, str(number), "edited", a["user_answer"]),
                           ensure_ascii=False) + "\n")
    return a


def set_decisions(project: str, decisions: list[dict]) -> dict:
    """ПАКЕТНОЕ принятие решений: [{number, status, user_answer?}, ...].

    Один load + один save + один append аудита вместо N полных перезаписей
    answers.json («Принять ВСЕ» на 75 замечаний — раньше 75 циклов чтения-записи)."""
    data = load_answers(project)
    by_num = {str(a.get("number")): a for a in data.get("answers", [])}
    applied: list[tuple[str, str, str | None]] = []
    for d in decisions:
        number = str(d.get("number"))
        status = d.get("status", "proposed")
        user_answer = d.get("user_answer")
        a = by_num.get(number)
        if a is None:
            continue
        a["status"] = status
        if user_answer is not None:
            a["user_answer"] = user_answer
        applied.append((number, status, user_answer))
    _save(project, data)
    # ИММУТАБЕЛЬНЫЙ АУДИТ (append-only): доказуемый след — что именно принято,
    # с каким текстом и источниками (снимок на момент решения).
    dec = project_paths(project)["decisions"]
    with dec.open("a", encoding="utf-8") as f:
        for number, status, user_answer in applied:
            f.write(json.dumps(_audit_entry(data, number, status, user_answer),
                               ensure_ascii=False) + "\n")
    for number, status, _ua in applied:
        if status in ("accepted", "edited"):
            _memorize(project, data, number)
    return data


def set_decision(project: str, number: str, *, status: str,
                 user_answer: str | None = None) -> dict:
    """Пользователь принимает/правит/отклоняет конкретное предложение."""
    return set_decisions(project, [{"number": number, "status": status,
                                    "user_answer": user_answer}])


# ─────────── фоновая генерация ответов (v0.33 — «как в индексации») ───────────
# Раньше «① Найти ответы» крутился в процессе интерфейса: час без прогресса,
# без пульса и стопа; обрыв вкладки убивал всё. Теперь — отдельный процесс с
# файлом состояния answers_state.json (прогресс/пульс/пауза), журналом
# answers_log.txt и возобновлением (готовые ответы не переспрашиваются).

_ANS_BG = False  # выставляется в True только в дочернем CLI-процессе


def _ans_state_path(project: str) -> Path:
    return project_paths(project)["root"] / "answers_state.json"


def read_answers_state(project: str) -> dict:
    p = _ans_state_path(project)
    if not p.exists():
        return {"status": "idle"}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"status": "idle"}


def _replace_atomic(tmp: Path, dst: Path) -> None:
    """tmp→dst с повторами: на Windows os.replace падает PermissionError, если
    другой процесс в этот миг читает dst (ревью; open() без FILE_SHARE_DELETE)."""
    import time
    for _ in range(6):
        try:
            tmp.replace(dst)
            return
        except PermissionError:
            time.sleep(0.15)
    tmp.replace(dst)


def write_answers_state(project: str, st: dict) -> None:
    import os
    import threading
    global _ANS_WRITE_LOCK
    try:
        _ANS_WRITE_LOCK
    except NameError:
        _ANS_WRITE_LOCK = threading.Lock()
    with _ANS_WRITE_LOCK:
        st["updated_at"] = st["heartbeat"] = datetime.now().isoformat(timespec="seconds")
        p = _ans_state_path(project)
        p.parent.mkdir(parents=True, exist_ok=True)
        # tmp уникален ДЛЯ ПРОЦЕССА (ревью: GUI и фон писали один tmp-путь —
        # межпроцессная коллизия при одновременной записи)
        tmp = p.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        _replace_atomic(tmp, p)


def _ans_progress(project: str, total: int, done: int, message: str,
                  status: str = "running") -> None:
    import os
    st = read_answers_state(project)
    st.update({"status": status, "total": int(total), "done": int(done),
               "message": message})
    if status == "running":
        st["pid"] = os.getpid() if _ANS_BG else int(st.get("pid") or 0)
    else:
        st["pid"] = 0
    write_answers_state(project, st)
    print(f"[block1] {message}", flush=True)


# Стоп — ОТДЕЛЬНЫМ файлом-флагом, а не полем в state (ревью: чтение-изменение-
# запись state из двух процессов затирало флаг — «Стоп» терялся).
def _ans_stop_flag(project: str) -> Path:
    return project_paths(project)["root"] / "answers_stop.flag"


def _ans_stop_requested(project: str) -> bool:
    return _ans_stop_flag(project).exists()


answers_stop_requested = _ans_stop_requested  # публичное имя для GUI


def request_answers_stop(project: str) -> None:
    try:
        _ans_stop_flag(project).touch()
    except OSError:
        pass


def clear_answers_stop(project: str) -> None:
    try:
        _ans_stop_flag(project).unlink(missing_ok=True)
    except OSError:
        pass


def answers_running(project: str) -> bool:
    """True = фоновая генерация жива (running/starting + свежий пульс).
    Пульс «из будущего» (перевод часов, файл с другой машины) считаем живым —
    иначе открывался бы двойной запуск (ревью)."""
    st = read_answers_state(project)
    if st.get("status") not in ("running", "starting"):
        return False
    try:
        age = (datetime.now() - datetime.fromisoformat(
            st.get("heartbeat") or "")).total_seconds()
    except (ValueError, TypeError):
        return False
    return age < 60


def stop_answers(project: str, *, hard: bool = False) -> bool:
    """«⏹ Стоп» (hard=False) — МЯГКО: флаг, генерация остановится после
    текущего пакета, всё готовое сохранено. «✋ Прервать» (hard=True) — жёстко:
    завершить процесс, но ТОЛЬКО опознанный как наш воркер (по командной
    строке) — не слепой kill по pid (ревью: pid мог переиспользоваться)."""
    import os
    import signal
    request_answers_stop(project)
    st = read_answers_state(project)
    if not hard:
        if st.get("status") in ("running", "starting"):
            st["message"] = ("⏹ Останавливаюсь после текущего пакета… "
                             "(готовые ответы сохранены)")
            write_answers_state(project, st)
        else:  # процесса нет — просто зафиксировать паузу
            st.update({"status": "paused", "pid": 0,
                       "message": "⏹ Остановлено. «⏯ Продолжить» доделает."})
            write_answers_state(project, st)
        return True
    pid = int(st.get("pid") or 0)
    if pid and pid != os.getpid():
        from ..core.session_guard import _cmdline
        if "block1_answers" in _cmdline(pid).lower():
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    st = read_answers_state(project)
    st.update({"status": "paused", "pid": 0,
               "message": "✋ Прервано. Пакет в работе не сохранился; "
                          "«⏯ Продолжить» доделает оставшиеся."})
    write_answers_state(project, st)
    return True


def answers_log_path(project: str) -> Path:
    return project_paths(project)["root"] / "answers_log.txt"


def start_answers_background(project: str, *, object_type: str | None = None,
                             remarks_path: str | None = None) -> int:
    """Запустить генерацию ответов ОТДЕЛЬНЫМ процессом (переживает перерисовки
    интерфейса и закрытие вкладки). Возвращает pid (0 = не удалось/уже идёт)."""
    import os
    import subprocess
    import sys
    from ..paths import APP_ROOT
    # ЗАЩИТА ОТ ДВОЙНОГО ЗАПУСКА (ревью, high): disabled-кнопка в интерфейсе не
    # останавливает второй клик, пришедший до перерисовки — два процесса
    # отвечали бы на одни замечания (двойной расход API).
    if answers_running(project):
        return 0
    st = read_answers_state(project)
    st.update({"status": "starting", "pid": 0,
               "message": "Запуск фонового процесса…"})
    write_answers_state(project, st)
    lp = answers_log_path(project)
    lp.parent.mkdir(parents=True, exist_ok=True)
    logf = open(lp, "ab")  # журнал ДОПИСЫВАЕТСЯ (история прогонов непрерывна)
    logf.write(f"\n===== {datetime.now().isoformat(timespec='seconds')} "
               f"генерация ответов: {project} =====\n".encode("utf-8"))
    logf.flush()
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    args = [sys.executable, "-m", "pmoos.pipeline.block1_answers",
            "--project", project]
    if object_type:
        args += ["--object-type", object_type]
    if remarks_path:
        args += ["--remarks", str(remarks_path)]
    kwargs: dict[str, Any] = {"env": env, "cwd": str(APP_ROOT),
                              "stdout": logf, "stderr": logf}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000200 | 0x00000008  # NEW_GROUP | DETACHED
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(args, **kwargs)
    except Exception as e:  # noqa: BLE001
        err = f"Не удалось запустить фоновый процесс: {e}"
        logf.write((err + "\n").encode("utf-8"))
        logf.close()
        st = read_answers_state(project)
        st.update({"status": "error", "pid": 0, "message": err})
        write_answers_state(project, st)
        return 0
    logf.close()  # дескриптор унаследован дочерним процессом
    return proc.pid


def _start_ans_heartbeat(project: str) -> None:
    """Пульс каждые 5 с — по нему видно, что процесс жив во время долгого
    вызова ИИ (reasoner может думать минутами)."""
    import os
    import threading
    import time

    def beat() -> None:
        while True:
            time.sleep(5)
            try:
                st = read_answers_state(project)
                if int(st.get("pid") or 0) != os.getpid():
                    return
                if st.get("status") != "running":
                    return
                write_answers_state(project, st)
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=beat, daemon=True).start()


def _main() -> None:
    import argparse
    import os
    import traceback
    global _ANS_BG
    ap = argparse.ArgumentParser(description="Фоновая генерация ответов STR.RAG (Блок 1)")
    ap.add_argument("--project", required=True)
    ap.add_argument("--object-type", default=None)
    ap.add_argument("--remarks", default=None)
    a = ap.parse_args()
    _ANS_BG = True
    print(f"[block1] старт: проект «{a.project}», pid={os.getpid()}", flush=True)
    st = read_answers_state(a.project)
    # самозащита от параллельного двойника: другой ЖИВОЙ pid уже отвечает
    _other = int(st.get("pid") or 0)
    if st.get("status") == "running" and _other and _other != os.getpid():
        try:
            age = (datetime.now() - datetime.fromisoformat(
                st.get("heartbeat") or "")).total_seconds()
        except (ValueError, TypeError):
            age = 1e9
        if age < 60:
            print(f"[block1] уже работает pid={_other} — выходим без дубля", flush=True)
            return
    st.update({"status": "running", "pid": os.getpid(),
               "message": "Подготовка (разбор файла замечаний)…"})
    write_answers_state(a.project, st)
    clear_answers_stop(a.project)   # старый флаг «Стоп» не глушит новый запуск
    _start_ans_heartbeat(a.project)
    try:
        run_block1(a.project, object_type=a.object_type, remarks_path=a.remarks)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        st = read_answers_state(a.project)
        st.update({"status": "error", "pid": 0,
                   "message": f"Ошибка: {e}. Готовые ответы сохранены — "
                              f"«⏯ Продолжить» доделает остальные."})
        write_answers_state(a.project, st)


if __name__ == "__main__":
    _main()
