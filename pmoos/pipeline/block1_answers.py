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

_SYS = (
    "Ты — главный инженер-эколог, готовишь ответы на замечания государственной "
    "экспертизы к разделу ПМООС/ООС проектной документации (Постановление "
    "Правительства РФ №87). Отвечай профессионально, по существу, со ссылками на "
    "конкретные данные проекта и действующие нормативы. Не выдумывай данные, "
    "которых нет в предоставленных фрагментах: если данных не хватает — прямо "
    "укажи, какой раздел/расчёт нужно дополнить."
)

_USER_TMPL = (
    "ЗАМЕЧАНИЕ ЭКСПЕРТА №{num}:\n«{remark}»\n\n"
    "НАЙДЕННЫЕ ФРАГМЕНТЫ ПРОЕКТНОЙ ДОКУМЕНТАЦИИ (источники):\n{context}\n\n"
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
    'если всё есть"\n'
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
        lines.append(f"[{i}] (раздел: {sec}; файл: {file}; место: {loc})\n{snippet}")
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

    if has("перерасч", "пересчит"):
        return "Перерасчёт"
    if has("расчёт", "расчет", "рассеиван") and has("уточн", "выполн", "привести",
                                                    "откоррект", "провести", "повтор"):
        return "Перерасчёт"
    if has("гост", "санпин", "снип", "гн 2", "сп 2", "сп 5", "норматив", "методик",
           "приказ", "постановлен", "-фз", "фз-", "в соответствии с требованиями"):
        return "Нормативы"
    if has("приложить", "представить", "предоставить", "лиценз", "договор", "справк",
           "письмо", "протокол", "паспорт отход", "сертификат"):
        return "Доп. документы"
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
            kw = [fp for fp in allf
                  if any(k in fp.name.lower() for k in ("замечан", "remark"))]
            cand = kw or (allf if len(allf) == 1 else
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
    src_codes = source_section_codes(object_type)
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
        for a in _answer_pack(project, cfg, object_type, pack, src_codes, progress):
            by_num[str(a["number"])] = a
        _save_merged(project, remarks, by_num, cfg, object_type, partial=True)
        _ans_progress(project, total, len(by_num),
                      f"Готово ответов: {len(by_num)}/{total}")

    out = _save_merged(project, remarks, by_num, cfg, object_type, partial=False)
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
        hits_per = retr.batch_search(project, queries, sections=src_codes or None,
                                     top=int(cfg.get("retrieval.top_k", 8)))
        # №10-5: к какому ТОМУ ООС относится замечание — лёгкий поиск top-1
        # только по разделу OOS (томов может быть несколько). Расширение запроса
        # не нужно; пул реранка сжат 60→16: этот пасс выбирает ЛИШЬ файл-том
        # (payload.file) для top-1, полный пул тратил ~половину rerank-бюджета
        # всего прогона на второстепенное поле.
        try:
            oos_per = retr.batch_search(
                project, queries, sections=["OOS"], top=1, use_expansion=False,
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

    # 3) формируем задания для ИИ (с few-shot из памяти прошлых проектов)
    use_mem = bool(cfg.get("memory.enabled", True))
    mem_k = int(cfg.get("memory.k", 2))
    jobs, ctx_sources = [], []
    for r, hits in zip(remarks, hits_per):
        ctx, srcs = _format_context(hits, limit=int(cfg.get("retrieval.top_k", 8)),
                                    max_chars=int(cfg.get("retrieval.snippet_chars", 3000)))
        ctx_sources.append((srcs, hits))
        user_msg = _USER_TMPL.format(num=r.number, remark=r.text, context=ctx or "(не найдено)")
        if use_mem:
            try:
                from ..memory import fewshot_block
                fs = fewshot_block(r.text, k=mem_k, exclude_project=project, cfg=cfg)
            except Exception:  # noqa: BLE001
                fs = ""
            if fs:
                user_msg = fs + "\n\n" + user_msg
        jobs.append([
            {"role": "system", "content": _SYS},
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
        confidence = data.get("confidence", "")
        if unsupported_refs or low_support:
            confidence = "low"

        # каскад: какие разделы затронет правка (по разделам источников; если ИИ не
        # атрибутировал — по найденным поиском, каскад носит справочный характер)
        affected_codes = sorted({s["section"] for s in (used_sources or srcs) if s.get("section")})
        cascade = downstream(project, affected_codes) if affected_codes else {"changed": [], "affected": []}

        answers.append({
            "number": r.number,
            "remark": r.text,
            "oos_volume": oos_by_num.get(str(r.number), ""),  # №10-5
            # №10-6: категория из файла замечаний (если была колонка), иначе —
            # автоматическая классификация по тексту замечания
            "category": (getattr(r, "category", "") or _classify_remark(r.text)),
            "answer": answer_text,
            "correction": data.get("correction", ""),
            # структурированная правка: где / было / стало / что приложить
            "edit_location": data.get("edit_location", ""),
            "edit_was": data.get("edit_was", ""),
            "edit_shall": data.get("edit_shall", ""),
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
