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
    '  "correction": "какую правку внести в раздел ПМООС (конкретно)",\n'
    '  "used_sources": [номера фрагментов, реально использованных, напр. [1,3]],\n'
    '  "confidence": "high|medium|low",\n'
    '  "missing_data": "чего не хватает в документации (или пусто)"\n'
    "}}\n"
    "Верни ТОЛЬКО JSON."
)


def _format_context(hits: list[dict], limit: int = 8) -> tuple[str, list[dict]]:
    lines, srcs = [], []
    for i, h in enumerate(hits[:limit], 1):
        pl = h.get("payload", {})
        loc = pl.get("loc", "")
        file = pl.get("file", "")
        sec = pl.get("section", "")
        snippet = (h.get("text", "") or "")[:900]
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
    for key in ("answer", "correction", "missing_data"):
        v = out.get(key)
        out[key] = v.strip() if isinstance(v, str) else ("" if v is None else str(v))
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
        # ищем файл замечаний: сначала постоянная папка remarks/, затем
        # (для обратной совместимости) старое место — tmp_uploads
        cand = []
        for folder in (paths.get("remarks_dir"), paths["uploads"]):
            if folder and folder.exists():
                for fp in sorted(folder.rglob("*")):
                    if fp.is_file() and any(k in fp.name.lower() for k in ("замечан", "remark")):
                        cand.append(fp)
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

    # 2) ресурсы и батчевый retrieval только по разделам-источникам
    retr = HybridRetriever(cfg)
    try:
        src_codes = source_section_codes(object_type)
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
        ctx, srcs = _format_context(hits, limit=int(cfg.get("retrieval.top_k", 8)))
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

    # СОХРАНЯЕМ РЕШЕНИЯ ПОЛЬЗОВАТЕЛЯ (находка аудита): повторный запуск Блока 1
    # раньше МОЛЧА затирал принятые/правленые ответы (status → proposed,
    # user_answer → None) без бэкапа. Принятое решение — финал: такие ответы
    # переносятся из прежнего файла как есть; свежая генерация заменяет только
    # непринятые (proposed/rejected).
    prev = load_answers(project) or {}
    kept = {str(a.get("number")): a for a in prev.get("answers", [])
            if a.get("status") in ("accepted", "edited")}
    if kept:
        answers = [kept.get(str(a.get("number")), a) for a in answers]
        n_kept = sum(1 for a in answers if a.get("status") in ("accepted", "edited"))
        print(f"[block1] сохранено принятых/правленых ответов: {n_kept}", flush=True)

    out = {
        "project": project, "object_type": object_type,
        "block": 1, "count": len(answers),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "provenance": _provenance(cfg, object_type),   # снимок пайплайна (атрибуция регрессий)
        "answers": answers,
    }
    _save(project, out)
    return out


def _save(project: str, data: dict) -> Path:
    p = project_paths(project)["answers"]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
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
