"""ГЕНЕРАЦИЯ РАЗДЕЛА ИИ по исходным данным ДРУГИХ разделов ПД (v0.48).

ТЗ: «по исходным данным из разделов проектной документации (без ООС/ИЭИ/
ОЦЕНКИ) осуществить генерацию этих разделов в автоматическом режиме».

Схема (честная, без выдумок):
  для каждой главы целевого раздела (структура — по эталонам пользователя,
  см. output/section_draft.CHAPTERS) → поиск по базе проекта фрагментов из
  разделов-источников (ТКР/ПОС/КР/ТХ/изыскания…) + показатели из ДАННЫХ →
  ИИ пишет текст главы ТОЛЬКО по этим фрагментам, каждое утверждение с
  пометкой источника [файл, стр.], а чего нет в данных — помечает
  «◈ ВНЕСТИ: …». Итог — docx с главами, источниками и ведомостью пробелов.

Работает ФОНОВЫМ процессом (как ответы на замечания): состояние в
section_gen_state.json (прогресс/пульс/стоп), журнал в section_gen_log.txt.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..paths import project_paths, APP_ROOT

_SYS = (
    "Ты — главный инженер проекта, пишешь ПОДРАЗДЕЛ раздела проектной документации "
    "«{target}» (Постановление Правительства РФ № 87, п. 25) по исходным данным "
    "других разделов проекта. Пиши официальным техническим языком, связным текстом "
    "с абзацами; таблицы — в markdown (| колонка | колонка |). "
    "СТРОГО: используй ТОЛЬКО факты из предоставленных фрагментов и показателей; "
    "после каждого факта ставь ссылку на источник в квадратных скобках вида "
    "[файл, место]. Ничего не выдумывай и не пиши общих слов «в соответствии с "
    "требованиями» без конкретики. ПЕРВОЙ СТРОКОЙ ответа дай вердикт: "
    "«СТАТУС: ДОСТАТОЧНО» — если данных хватает написать подраздел по существу, "
    "или «СТАТУС: НЕДОСТАТОЧНО — нужно: …» (перечисли, какие документы/данные и "
    "из какого раздела нужны) — тогда текст подраздела НЕ пиши. Отдельные "
    "пробелы внутри достаточного текста помечай строкой «◈ ВНЕСТИ: …». "
    "Не повторяй текст замечаний экспертизы, не пиши вступлений и извинений."
)
_USER = (
    "РАЗДЕЛ: {target}\nПОДРАЗДЕЛ {n}: {chapter}\n"
    "ЧТО ДОЛЖЕН СОДЕРЖАТЬ (по форме раздела): {needs}\n\n"
    "ПОКАЗАТЕЛИ ПРОЕКТА ИЗ БАЗЫ (значение · источник):\n{indicators}\n\n"
    "ФРАГМЕНТЫ РАЗДЕЛОВ-ИСТОЧНИКОВ:\n{fragments}\n\n"
    "Сначала строка «СТАТУС: …», затем (если ДОСТАТОЧНО) текст подраздела "
    "«{chapter}» (300–1200 слов)."
)

# ─────────────────── ФОРМА РАЗДЕЛА ООС ───────────────────
# По принятому эталону пользователя «Раздел ПД №8 АК-01-25-ООС» (ООО «ИНТЕГРА»,
# 337 стр., OneDrive\Формы\Разработка\ООС — оглавление тома) и п. 25 ПП РФ № 87
# (текстовая часть: результаты оценки воздействия; перечень мероприятий по
# предотвращению/снижению воздействия — воздух, воды, земли/почвы, отходы,
# растительный и животный мир, физические факторы; ПЭК; затраты; графическая
# часть). needs — что нужно для генерации подраздела, keys — слова-признаки
# исходных данных (подсказка ИИ и ведомости).
OOS_FORM: list[dict] = [
    {"n": "1", "title": "Введение", "subs": [
        {"n": "1", "title": "Введение (основание, цель, состав исходных данных)",
         "needs": "ПЗ: наименование и адрес объекта, заказчик, основание для "
                  "проектирования (ТЗ), перечень исходных данных (отчёты изысканий)"}]},
    {"n": "2", "title": "Характеристика природных условий района", "subs": [
        {"n": "2.1", "title": "Местоположение и рельеф",
         "needs": "ПЗ/ПЗУ (ППО): местоположение участка; ИГИ/ИЭИ: геоморфология, рельеф, отметки"},
        {"n": "2.2", "title": "Климатические характеристики",
         "needs": "ИЭИ/ИГМИ или справка ЦГМС о климате: температуры, осадки, ветер (роза), "
                  "коэффициент А, скорость ветра 5 %, глубина промерзания"},
        {"n": "2.3", "title": "Геолого-гидрогеологическая характеристика территории",
         "needs": "ИГИ: геологическое строение, грунты, уровень грунтовых вод, опасные процессы"},
        {"n": "2.4", "title": "Характеристика почвенного покрова",
         "needs": "ИЭИ: типы почв, мощность плодородного слоя, загрязнение почв (протоколы)"},
        {"n": "2.5", "title": "Современное состояние окружающей среды (воздух, почвы, "
                               "радиационная обстановка, физические факторы)",
         "needs": "ИЭИ: справка о фоновых концентрациях, протоколы измерений воздуха, почв, "
                  "радиационного обследования, шума"},
        {"n": "2.6", "title": "Характеристика растительного и животного мира",
         "needs": "ИЭИ: растительность, животный мир, редкие и охраняемые виды (Красные книги)"},
        {"n": "2.7", "title": "Состояние водных ресурсов",
         "needs": "ИГМИ/ИЭИ: водные объекты, водоохранные зоны, гидрологический режим, качество вод"},
        {"n": "2.8", "title": "Особо охраняемые природные территории и зоны с особыми "
                               "условиями использования территорий",
         "needs": "ИЭИ, письма уполномоченных органов: ООПТ, ЗОУИТ, ОКН, СЗЗ, ЗСО, водоохранные зоны"}]},
    {"n": "3", "title": "Проектные решения", "subs": [
        {"n": "3", "title": "Проектные решения (краткая характеристика объекта, технологии, "
                            "организация строительства)",
         "needs": "ПЗ, ПЗУ/ППО, АР/ТКР, ИОС, ПОС: состав объекта, технико-экономические "
                  "показатели, технологии, сроки и организация строительства, техника"}]},
    {"n": "4", "title": "Охрана земельных ресурсов", "subs": [
        {"n": "4.1", "title": "Оценка воздействия объекта на земельные ресурсы",
         "needs": "ПЗУ/ППО, ПОС: площади постоянного и временного отвода, категории земель, "
                  "снятие плодородного слоя, нарушение земель"},
        {"n": "4.2", "title": "Мероприятия по охране земельных ресурсов",
         "needs": "ПОС/ППО: рекультивация, хранение ПСП, защита от загрязнения и эрозии"}]},
    {"n": "5", "title": "Охрана атмосферного воздуха", "subs": [
        {"n": "5.1", "title": "Оценка воздействия на атмосферный воздух в период строительства",
         "needs": "расчёт выбросов на период строительства (ИЗА, вещества, г/с и т/год), "
                  "расчёт рассеивания, ПОС: техника и объёмы работ, справка о фоне"},
        {"n": "5.2", "title": "Оценка воздействия на атмосферный воздух в период эксплуатации",
         "needs": "расчёт выбросов на период эксплуатации (источники, вещества), расчёт "
                  "рассеивания, ИОС: котельная, вентиляция, стоянки"},
        {"n": "5.3", "title": "Мероприятия по охране атмосферного воздуха",
         "needs": "ПОС и ИОС: пылеподавление, режим работы техники, газоочистка; мероприятия при НМУ"},
        {"n": "5.4", "title": "Предложения по нормативам допустимых выбросов",
         "needs": "результаты расчёта рассеивания: перечень веществ и ИЗА с предлагаемыми "
                  "нормативами г/с и т/год"}]},
    {"n": "6", "title": "Мероприятия по охране и рациональному использованию водных ресурсов", "subs": [
        {"n": "6.1", "title": "Оценка воздействия на водные ресурсы в период строительства",
         "needs": "ПОС: водопотребление и водоотведение стройплощадки, поверхностный сток, "
                  "ИГМИ: водные объекты и водоохранные зоны"},
        {"n": "6.2", "title": "Оценка воздействия на водные ресурсы в период эксплуатации",
         "needs": "ИОС (ВК/НВК): балансы водопотребления и водоотведения, очистные "
                  "сооружения, качество стоков, договор на приём стоков"},
        {"n": "6.3", "title": "Мероприятия по охране поверхностных и подземных вод от истощения "
                               "и загрязнения",
         "needs": "ИОС/ПОС: очистка стоков, водоотвод, защита водных объектов"},
        {"n": "6.4", "title": "Мероприятия по предотвращению вторичного загрязнения воды систем "
                               "хозяйственно-питьевого водоснабжения",
         "needs": "ИОС (ВК): источник водоснабжения, ЗСО, материалы труб, обеззараживание"}]},
    {"n": "7", "title": "Охрана растительного и животного мира", "subs": [
        {"n": "7.1", "title": "Оценка воздействия и мероприятия по охране растительного и животного мира",
         "needs": "ИЭИ: растительность и животный мир участка, ПЗУ/ППО: вырубка, "
                  "компенсационное озеленение, ограждения, сроки работ"}]},
    {"n": "8", "title": "Акустическое воздействие объекта на прилегающую территорию", "subs": [
        {"n": "8.1", "title": "Оценка акустического воздействия строительных работ",
         "needs": "расчёт шума на период строительства, ПОС: техника, режим работ, "
                  "расстояния до жилой застройки"},
        {"n": "8.2", "title": "Оценка акустического воздействия при эксплуатации объекта",
         "needs": "расчёт шума на период эксплуатации, ИОС: источники шума (вентиляция, "
                  "трансформаторы, транспорт), протоколы замеров"}]},
    {"n": "9", "title": "Мероприятия по сбору, использованию, обезвреживанию, транспортировке "
                        "и размещению отходов", "subs": [
        {"n": "9.1", "title": "Перечень и характеристики отходов периода строительства",
         "needs": "ПОС/СМ: ведомость объёмов работ, материалы; расчёт образования отходов, "
                  "коды ФККО, классы опасности"},
        {"n": "9.2", "title": "Мероприятия по снижению воздействия строительных отходов",
         "needs": "ПОС: накопление, вывоз, договоры/КП на приём отходов"},
        {"n": "9.3", "title": "Перечень и характеристики отходов периода эксплуатации",
         "needs": "ТХ/ИОС: технологические процессы, численность персонала, нормативы образования"},
        {"n": "9.4", "title": "Мероприятия по снижению воздействия отходов в период эксплуатации",
         "needs": "ИОС/ПЗУ: площадки накопления, контейнеры, договоры на вывоз"}]},
    {"n": "10", "title": "Перечень затрат на реализацию природоохранных мероприятий и "
                         "компенсационных выплат", "subs": [
        {"n": "10.1", "title": "Оценка компенсационных выплат за выбросы в атмосферный воздух",
         "needs": "валовые выбросы т/год по веществам (строительство и эксплуатация), ставки платы"},
        {"n": "10.2", "title": "Оценка компенсационных выплат за размещение отходов",
         "needs": "количество отходов т/год по классам опасности, ставки платы"}]},
    {"n": "11", "title": "Программа производственного экологического контроля (мониторинга)", "subs": [
        {"n": "11.1", "title": "Программа ПЭК в период строительства",
         "needs": "результаты глав 5–9: контролируемые показатели, точки, периодичность"},
        {"n": "11.2", "title": "Программа ПЭК в период эксплуатации, план-график",
         "needs": "ИОС/ТХ: источники воздействия, точки контроля, периодичность, исполнители"}]},
]
OOS_APPENDICES_TEXT = [
    "Справка о климатических характеристиках", "Справка о фоновых концентрациях / "
    "протокол измерений атмосферного воздуха", "Письма уполномоченных органов (ООПТ, "
    "ЗОУИТ, ОКН)", "Ведомость основных объёмов работ", "Расчёт выбросов ЗВ на период "
    "строительства", "Расчёт рассеивания ЗВ на период строительства", "Расчёт выбросов ЗВ "
    "на период эксплуатации", "Расчёт рассеивания ЗВ на период эксплуатации", "Расчёт "
    "акустического воздействия на период строительства", "Расчёт акустического воздействия "
    "на период эксплуатации", "Протоколы замеров уровня шума", "Договор на приём стоков",
    "Договор / КП на приём отходов",
]
OOS_APPENDICES_GRAPHIC = [
    "Ситуационный план", "Карта-схема источников выбросов ЗВ, источников шума и расчётных "
    "точек на период строительства", "Карта-схема источников выбросов ЗВ, источников шума и "
    "расчётных точек на период эксплуатации",
]


def form_units(target: str) -> list[dict]:
    """Единицы генерации: для ООС — подразделы формы (с заголовками глав), для
    остальных разделов — главы CHAPTERS без подразделов."""
    from ..output.section_draft import CHAPTERS
    units: list[dict] = []
    if target == "OOS":
        for ch in OOS_FORM:
            single = len(ch["subs"]) == 1 and ch["subs"][0]["n"] == ch["n"]
            for i, s in enumerate(ch["subs"]):
                units.append({"n": s["n"], "title": s["title"], "needs": s["needs"],
                              "chapter": ch["title"] if (i == 0 and not single) else "",
                              "chapter_n": ch["n"], "level": 1 if single else 2})
        return units
    chapters = CHAPTERS.get(target) or CHAPTERS["OOS"]
    for i, c in enumerate(chapters, start=1):
        units.append({"n": str(i), "title": c, "needs": "исходные данные других разделов "
                      "проекта и изысканий по теме главы", "chapter": "", "chapter_n": str(i),
                      "level": 1})
    return units


_STATUS_RX = re.compile(r"^\s*СТАТУС\s*:\s*(ДОСТАТОЧНО|НЕДОСТАТОЧНО)[^\n]*", re.I | re.M)


def _parse_status(text: str) -> tuple[bool, str, str]:
    """(достаточно?, что нужно, текст без строки статуса)."""
    m = _STATUS_RX.search(text or "")
    if not m:
        return True, "", (text or "").strip()
    ok = m.group(1).upper() == "ДОСТАТОЧНО"
    line = m.group(0)
    need = re.sub(r"^\s*СТАТУС\s*:\s*НЕДОСТАТОЧНО\s*[—\-–:]*\s*(нужно\s*:)?\s*", "", line,
                  flags=re.I).strip()
    rest = (text[:m.start()] + text[m.end():]).strip()
    return ok, need, rest


def placeholder_text(unit: dict, extra_need: str = "", hits: int = 0) -> str:
    need = unit.get("needs", "")
    if extra_need:
        need = f"{extra_need}. По форме раздела также: {need}"
    reason = ("в базе проекта не найдено фрагментов по теме подраздела" if hits == 0
              else f"найденных фрагментов ({hits}) недостаточно для текста по существу")
    return (f"◈ ПОДРАЗДЕЛ НЕ СФОРМИРОВАН: {reason}.\n"
            f"Для генерации добавьте в базу проекта: {need}.")


# ───────────────────── состояние фонового процесса ─────────────────────
def _state_path(project: str) -> Path:
    return project_paths(project)["root"] / "section_gen_state.json"


def log_path(project: str) -> Path:
    return project_paths(project)["root"] / "section_gen_log.txt"


def _stop_path(project: str) -> Path:
    return project_paths(project)["root"] / "section_gen_stop.flag"


def read_state(project: str) -> dict:
    p = _state_path(project)
    if not p.exists():
        return {"status": "idle", "total": 0, "done": 0, "message": "", "pid": 0}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"status": "idle", "total": 0, "done": 0, "message": "", "pid": 0}


def write_state(project: str, st: dict) -> None:
    p = _state_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    st["heartbeat"] = datetime.now().isoformat(timespec="seconds")
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    for _ in range(5):
        try:
            tmp.replace(p)
            return
        except PermissionError:
            time.sleep(0.2)
    tmp.replace(p)


def is_running(project: str) -> bool:
    st = read_state(project)
    if st.get("status") not in ("running", "starting"):
        return False
    try:
        age = (datetime.now() - datetime.fromisoformat(st.get("heartbeat") or "")).total_seconds()
    except (ValueError, TypeError):
        return False
    return age < 90


def stop_generation(project: str) -> bool:
    _stop_path(project).write_text("stop", encoding="utf-8")
    return True


def _progress(project: str, total: int, done: int, message: str, status: str = "running",
              **extra) -> None:
    st = read_state(project)
    st.update({"status": status, "total": total, "done": done, "message": message,
               "pid": os.getpid() if status == "running" else st.get("pid", 0)})
    st.update(extra)
    write_state(project, st)


# ───────────────────── сбор данных для главы ─────────────────────
def _indicators_text(project: str) -> str:
    try:
        from ..data import registry as R
        inds = (R.load_registry(project).get("indicators") or {})
    except Exception:  # noqa: BLE001
        return "(показатели не собраны)"
    lines = []
    for m in R.INDICATORS:
        rec = inds.get(m["key"], {})
        v = str(rec.get("value") or "").strip()
        if not v:
            continue
        prov = rec.get("provenance") or {}
        lines.append(f"- {m['label']}: {v} {rec.get('unit', m['unit'])} · "
                     f"[{prov.get('file', '—')}, {prov.get('loc', '')}]")
    return "\n".join(lines) or "(показатели не собраны — соберите во вкладке ДАННЫЕ)"


def _fragments_text(hits: list[dict], limit_chars: int = 9000) -> str:
    out, total = [], 0
    for h in hits:
        pl = h.get("payload") or {}
        txt = re.sub(r"\s+", " ", (h.get("text") or pl.get("text") or "")).strip()
        if not txt:
            continue
        piece = f"[{pl.get('file', '?')}, {pl.get('loc', '')}] {txt[:900]}"
        if total + len(piece) > limit_chars:
            break
        out.append(piece)
        total += len(piece)
    return "\n\n".join(out) or "(фрагменты не найдены)"


def default_retriever(cfg, project: str, object_type: str, target: str) -> Callable[[str], list[dict]]:
    """Поиск по базе проекта по разделам-источникам целевого раздела.
    Возвращает функцию query → hits и объект для close()."""
    from ..retrieval.hybrid import HybridRetriever
    retr = HybridRetriever(cfg)
    # ВСЯ база проекта, кроме самого генерируемого раздела (07.09.2026: не
    # только разделы с флагом is_source — ППО/ТКР/ОДИ/ПБ тоже исходные данные)
    exclude = [target] if target else None

    def run(query: str) -> list[dict]:
        try:
            return retr.batch_search(project, [query], sections=None,
                                     exclude_sections=exclude,
                                     top=int(cfg.get("gen.top_k", 12)))[0]
        except Exception as e:  # noqa: BLE001
            print(f"[gen] поиск: {e}", flush=True)
            return []
    run.close = retr.close  # type: ignore[attr-defined]
    return run


# ───────────────────── основной проход ─────────────────────
def run_section_gen(project: str, target: str = "OOS", *, cfg=None,
                    object_type: str | None = None,
                    retrieve: Callable[[str], list[dict]] | None = None,
                    chat: Callable[..., str] | None = None) -> Path:
    from ..config import load_config
    from ..ingest.sections import target_name
    from ..output.section_draft import CHAPTERS
    cfg = cfg or load_config()
    if object_type is None:
        try:
            from ..index.indexer import read_state as _rs
            object_type = _rs(project).get("object_type") or "площадной"
        except Exception:  # noqa: BLE001
            object_type = "площадной"
    if chat is None:
        from ..core.ai_providers import chat as _chat
        chat = _chat
    own_retr = retrieve is None
    if retrieve is None:
        retrieve = default_retriever(cfg, project, object_type, target)
    units = form_units(target)
    tname = target_name(target)
    indicators = _indicators_text(project)
    total = len(units)
    results: list[dict] = []
    stop = _stop_path(project)
    if stop.exists():
        stop.unlink()
    try:
        for k, u in enumerate(units, start=1):
            n, chapter = u["n"], u["title"]
            if stop.exists():
                _progress(project, total, k - 1, f"⏹ Остановлено на подразделе {n}.",
                          status="paused")
                break
            _progress(project, total, k - 1,
                      f"Подраздел {n} ({k}/{total}): поиск данных — «{chapter[:60]}»")
            query = f"{chapter}. {u.get('needs', '')[:120]}"
            hits = retrieve(query)
            srcs = []
            for h in hits:
                pl = h.get("payload") or {}
                s = f"{pl.get('file', '')} {pl.get('loc', '')}".strip()
                if s and s not in srcs:
                    srcs.append(s)
            res = {"n": n, "chapter": chapter, "level": u["level"],
                   "chapter_title": u.get("chapter", ""), "chapter_n": u.get("chapter_n", ""),
                   "sources": srcs[:12], "hits": len(hits), "placeholder": False,
                   "needs": u.get("needs", "")}
            if not hits:
                # ДАННЫХ НЕТ — ИИ не зовём, подраздел остаётся пустым с перечнем,
                # что добавить (ТЗ 08.09: «где информации не хватает — раздел
                # оставлять пустым и писать, что нужно добавить»)
                res.update(text=placeholder_text(u, hits=0), placeholder=True)
                results.append(res)
                _progress(project, total, k, f"Подраздел {n}: данных нет — оставлен пустым.")
                continue
            _progress(project, total, k - 1,
                      f"Подраздел {n} ({k}/{total}): ИИ пишет — «{chapter[:60]}»")
            msgs = [{"role": "system", "content": _SYS.format(target=tname)},
                    {"role": "user", "content": _USER.format(
                        target=tname, n=n, chapter=chapter, needs=u.get("needs", ""),
                        indicators=indicators, fragments=_fragments_text(hits))}]
            try:
                raw = chat(cfg, msgs, module="module4", max_tokens=3500) or ""
            except Exception as e:  # noqa: BLE001
                raw = (f"СТАТУС: НЕДОСТАТОЧНО — нужно: повторить генерацию, ИИ недоступен "
                       f"({str(e)[:160]})")
            ok, need, text = _parse_status(raw)
            if not ok or not text.strip():
                res.update(text=placeholder_text(u, need, hits=len(hits)), placeholder=True,
                           ai_need=need)
            else:
                res.update(text=text.strip())
            results.append(res)
            _progress(project, total, k, f"Подраздел {n} ({k}/{total}) готов"
                      + (" (пустой — данных не хватает)." if res["placeholder"] else "."))
    finally:
        if own_retr:
            try:
                retrieve.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
    out = _write_docx(project, target, tname, results, indicators)
    st = read_state(project)
    done_all = len(results) == total
    _progress(project, total, len(results),
              (f"Готово: {len(results)} глав → {out.name}" if done_all
               else f"Остановлено: {len(results)}/{total} глав сохранено → {out.name}"),
              status="done" if done_all else "paused", output=str(out))
    return out


def _write_docx(project: str, target: str, tname: str, results: list[dict],
                indicators: str) -> Path:
    from docx import Document
    from docx.shared import Pt, RGBColor
    from ..output.common import add_heading, add_title, set_default_font
    from ..output.docx_writer import _is_md_table, _md_table_rows
    doc = Document()
    set_default_font(doc)
    add_title(doc, f"{tname} — проект раздела, сформированный ИИ ({project})")
    p = doc.add_paragraph(
        "Текст сформирован системой STR.RAG по данным других разделов проектной "
        "документации и показателям из базы проекта. Каждое утверждение снабжено "
        "ссылкой на источник; места, где данных нет, помечены «◈ ВНЕСТИ». Документ "
        "требует проверки инженером и нормоконтроля.")
    p.runs[0].font.size = Pt(9)
    p.runs[0].font.color.rgb = RGBColor(0x66, 0x66, 0x66)
    from docx.enum.text import WD_COLOR_INDEX
    gaps: list[str] = []
    empty: list[dict] = []
    for r in results:
        if r.get("chapter_title"):
            add_heading(doc, f"{r.get('chapter_n', '')}. {r['chapter_title']}", level=1)
        add_heading(doc, f"{r['n']}. {r['chapter']}", level=int(r.get("level", 1)))
        if r.get("placeholder"):
            # ПУСТОЙ ПОДРАЗДЕЛ: выделенная заглушка с тем, что нужно добавить
            for line in (r["text"] or "").splitlines():
                if not line.strip():
                    continue
                pp = doc.add_paragraph()
                rr = pp.add_run(line.strip())
                rr.font.highlight_color = WD_COLOR_INDEX.YELLOW
                rr.font.color.rgb = RGBColor(0xB0, 0x30, 0x00)
            empty.append(r)
            continue
        block: list[str] = []
        for line in (r["text"] or "").splitlines():
            if line.strip():
                block.append(line)
            else:
                _flush(doc, block, _is_md_table, _md_table_rows)
                block = []
        _flush(doc, block, _is_md_table, _md_table_rows)
        for line in (r["text"] or "").splitlines():
            if "◈ ВНЕСТИ" in line:
                gaps.append(f"Подраздел {r['n']}: {line.strip()[:200]}")
        if r["sources"]:
            ps = doc.add_paragraph("Источники: " + "; ".join(r["sources"]))
            ps.runs[0].font.size = Pt(8)
            ps.runs[0].font.color.rgb = RGBColor(0x55, 0x55, 0x55)
    add_heading(doc, "Список литературы", level=1)
    pl = doc.add_paragraph()
    rl = pl.add_run("◈ ВНЕСТИ: перечень нормативных документов и источников, "
                    "использованных в разделе (ПП РФ № 87, ФЗ-7, ФЗ-96, ФЗ-89, СанПиН "
                    "1.2.3685-21, СП, методики расчётов).")
    rl.font.highlight_color = WD_COLOR_INDEX.YELLOW
    if target == "OOS":
        add_heading(doc, "Приложения (текстовая часть)", level=1)
        for i, name in enumerate(OOS_APPENDICES_TEXT, start=1):
            pa = doc.add_paragraph()
            ra = pa.add_run(f"Приложение {i}. {name} — ЗАРЕЗЕРВИРОВАНО: вложить документ.")
            ra.font.highlight_color = WD_COLOR_INDEX.YELLOW
        add_heading(doc, "Приложения (графическая часть)", level=1)
        for i, name in enumerate(OOS_APPENDICES_GRAPHIC, start=1):
            pa = doc.add_paragraph()
            ra = pa.add_run(f"Приложение {i}. {name} — ЗАРЕЗЕРВИРОВАНО: вложить чертёж.")
            ra.font.highlight_color = WD_COLOR_INDEX.YELLOW
    add_heading(doc, "Показатели проекта, использованные при генерации", level=1)
    for line in indicators.splitlines():
        doc.add_paragraph(line.lstrip("- "))
    if empty:
        add_heading(doc, "Ведомость: что добавить, чтобы сформировать пустые подразделы",
                    level=1)
        tbl = doc.add_table(rows=1, cols=2)
        try:
            tbl.style = "Table Grid"
        except KeyError:
            pass
        tbl.rows[0].cells[0].text = "Подраздел"
        tbl.rows[0].cells[1].text = "Что добавить в базу проекта"
        for r in empty:
            c = tbl.add_row().cells
            c[0].text = f"{r['n']}. {r['chapter']}"
            c[1].text = (r.get("ai_need") or "") + (". " if r.get("ai_need") else "") + r.get("needs", "")
    if gaps:
        add_heading(doc, "Ведомость недостающих данных внутри текста (◈ ВНЕСТИ)", level=1)
        for g in gaps:
            pg = doc.add_paragraph(g)
            pg.runs[0].font.color.rgb = RGBColor(0xB0, 0x30, 0x00)
    out = project_paths(project)["out"] / f"ГЕНЕРАЦИЯ_{target}_{project}.docx".replace("/", "_")
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))
    return out


def _flush(doc, block: list[str], is_table, rows_fn) -> None:
    if not block:
        return
    text = "\n".join(block)
    if is_table(text):
        rows = rows_fn(text)
        if rows:
            ncols = max(len(r) for r in rows)
            tbl = doc.add_table(rows=len(rows), cols=ncols)
            try:
                tbl.style = "Table Grid"
            except KeyError:
                pass
            for i, row in enumerate(rows):
                for j in range(ncols):
                    tbl.cell(i, j).text = row[j] if j < len(row) else ""
            doc.add_paragraph()
            return
    for line in block:
        line = re.sub(r"^\s*(#+\s*|\*\*|__)", "", line).strip()
        doc.add_paragraph(line)


# ───────────────────── фоновый запуск ─────────────────────
def start_background(project: str, target: str = "OOS",
                     object_type: str | None = None) -> int:
    import subprocess
    if is_running(project):
        return 0
    try:
        from ..index.indexer import is_running as _idx
        if _idx(project):
            raise RuntimeError("идёт индексация — генерация после её завершения")
    except ImportError:
        pass
    st = read_state(project)
    st.update({"status": "starting", "pid": 0, "total": 0, "done": 0,
               "message": "Запуск фонового процесса…", "target": target, "output": ""})
    write_state(project, st)
    lp = log_path(project)
    lp.parent.mkdir(parents=True, exist_ok=True)
    logf = open(lp, "ab")
    logf.write(f"\n===== {datetime.now().isoformat(timespec='seconds')} генерация "
               f"{target}: {project} =====\n".encode("utf-8"))
    logf.flush()
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    args = [sys.executable, "-m", "pmoos.pipeline.section_gen",
            "--project", project, "--target", target]
    if object_type:
        args += ["--object-type", object_type]
    kwargs: dict[str, Any] = {"env": env, "cwd": str(APP_ROOT), "stdout": logf, "stderr": logf}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000200 | 0x00000008
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(args, **kwargs)
    except Exception as e:  # noqa: BLE001
        logf.write(f"не удалось запустить: {e}\n".encode("utf-8"))
        logf.close()
        st.update({"status": "error", "message": f"Не удалось запустить фоновый процесс: {e}"})
        write_state(project, st)
        return 0
    logf.close()
    return proc.pid


def _heartbeat(project: str) -> None:
    import threading

    def beat() -> None:
        while True:
            time.sleep(5)
            try:
                st = read_state(project)
                if int(st.get("pid") or 0) != os.getpid() or st.get("status") != "running":
                    return
                write_state(project, st)
            except Exception:  # noqa: BLE001
                pass
    threading.Thread(target=beat, daemon=True).start()


def _main() -> None:
    import argparse
    import traceback
    ap = argparse.ArgumentParser(description="Фоновая генерация раздела STR.RAG")
    ap.add_argument("--project", required=True)
    ap.add_argument("--target", default="OOS")
    ap.add_argument("--object-type", default=None)
    a = ap.parse_args()
    print(f"[gen] старт: {a.project} / {a.target}, pid={os.getpid()}", flush=True)
    st = read_state(a.project)
    st.update({"status": "running", "pid": os.getpid(), "message": "Подготовка…", "target": a.target})
    write_state(a.project, st)
    _heartbeat(a.project)
    try:
        run_section_gen(a.project, a.target, object_type=a.object_type)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        st = read_state(a.project)
        st.update({"status": "error", "pid": 0, "message": f"Ошибка: {e}"})
        write_state(a.project, st)


if __name__ == "__main__":
    _main()
