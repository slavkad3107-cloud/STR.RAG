"""Реестр ПРОЕКТНЫХ ПОКАЗАТЕЛЕЙ (фаза 1 по ТЗ 27-30.07.2026).

Смысл: «упор на данные, а не на документы». Из уже проиндексированных томов
вытаскиваются числовые показатели проекта (площади, мощности, сроки, выбросы,
отходы, водопотребление, СЗЗ…) вместе с ПРОВЕНАНСОМ — файл, раздел, страница.
Значения складываются в базу проекта (registry.json), а не в файлы; их можно
править вручную, а при расхождении значений между разделами — выбрать нужное.

Извлечение ДЕТЕРМИНИРОВАННОЕ (регулярные выражения по синонимам показателя),
без ИИ: цифра в документе экспертизы — не то место, где допустимы догадки
модели. ИИ подключается отдельно и только как подсказка (см. suggest_missing).
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ..paths import project_paths

# ── КАТАЛОГ ПОКАЗАТЕЛЕЙ ─────────────────────────────────────────────────────
# key: (человеческое имя, единица, синонимы для поиска в тексте)
# range — диапазон ПРАВДОПОДОБНЫХ значений (отсекает мусор вроде «трасса
# 100100 км», пришедший из номера/координаты); prefer — разделы, которые
# «владеют» показателем: при расхождении их значение весомее.
INDICATORS: list[dict] = [
    {"key": "area_build", "label": "Площадь застройки", "unit": "м²",
     "range": (1, 5_000_000), "prefer": ["PZU", "PZ", "POS"],
     "syn": [r"площад\w* застройки"]},
    {"key": "area_plot", "label": "Площадь участка (землеотвода)", "unit": "м²",
     "range": (1, 50_000_000), "prefer": ["PPO", "PZU", "PZ"],
     "syn": [r"площад\w* (?:земельного )?участка", r"площад\w* землеотвода",
             r"площад\w* полосы отвода"]},
    {"key": "length_route", "label": "Протяжённость трассы", "unit": "км",
     "range": (0.01, 2_000), "prefer": ["PPO", "TKR", "PZ"],
     "syn": [r"протяж[её]нност\w*", r"длина трассы"]},
    {"key": "duration", "label": "Продолжительность строительства", "unit": "мес.",
     "range": (0.5, 120), "prefer": ["POS"],
     "syn": [r"продолжительност\w* строительства", r"срок строительства"]},
    {"key": "workers", "label": "Численность работающих", "unit": "чел.",
     "range": (1, 5_000), "prefer": ["POS"],
     "syn": [r"численност\w* работающих", r"количество (?:рабочих|строителей|работающих)",
             r"общая численност\w*"]},
    {"key": "emission_year", "label": "Валовый выброс ЗВ", "unit": "т/год",
     "range": (0.0001, 100_000), "prefer": ["OOS"],
     "syn": [r"валов\w* выброс\w*", r"суммарн\w* выброс\w*", r"всего выброс\w*",
             r"итого .{0,25}выброс\w*", r"выброс\w* .{0,30}т/год",
             r"масса выброс\w*"]},
    {"key": "emission_sec", "label": "Максимально-разовый выброс", "unit": "г/с",
     "range": (0.000001, 10_000), "prefer": ["OOS"],
     "syn": [r"максимальн\w*[- ]разов\w* выброс\w*", r"выброс\w* .{0,30}г/с",
             r"итого .{0,25}г/с", r"всего .{0,20}г/с"]},
    {"key": "sources_count", "label": "Количество источников выбросов", "unit": "шт.",
     "range": (1, 10_000), "prefer": ["OOS"],
     "syn": [r"количество источников выброс\w*", r"источник\w* выброс\w* .{0,20}шт",
             r"всего источник\w*"]},
    {"key": "waste_year", "label": "Количество отходов", "unit": "т/год",
     "range": (0.0001, 1_000_000), "prefer": ["OOS"],
     "syn": [r"количество отходов", r"образован\w* отходов", r"объ[её]м отходов",
             r"итого .{0,20}отход\w*", r"всего отход\w*", r"масса отход\w*"]},
    {"key": "water_use", "label": "Водопотребление", "unit": "м³/сут",
     "range": (0.001, 100_000), "prefer": ["OOS", "IOS_VK", "POS"],
     "syn": [r"водопотреблени\w*", r"потребность в воде", r"расход воды"]},
    {"key": "water_drain", "label": "Водоотведение", "unit": "м³/сут",
     "range": (0.001, 100_000), "prefer": ["OOS", "IOS_VK", "POS"],
     "syn": [r"водоотведени\w*", r"сброс сточных вод", r"объ[её]м сточных вод"]},
    {"key": "szz", "label": "Размер санитарно-защитной зоны", "unit": "м",
     "range": (5, 3_000), "prefer": ["OOS", "IEI"],
     "syn": [r"санитарно[- ]защитн\w* зон\w*", r"размер сзз", r"санитарн\w* разрыв"]},
    {"key": "noise", "label": "Уровень шума", "unit": "дБА",
     "range": (20, 140), "prefer": ["OOS"],
     "syn": [r"уровен\w* шума", r"шумов\w* воздействи\w*", r"эквивалентн\w* уровен\w*"]},
]

_BY_KEY = {i["key"]: i for i in INDICATORS}

# число с единицей рядом: «12,345 т/год», «1 234.5 м2», «45 дБА»
_NUM = r"\d[\d  ]{0,12}(?:[.,]\d+)?"
_UNIT_ALIASES = {
    "м²": [r"м2", r"м²", r"кв\.?\s*м"], "км": [r"км"], "мес.": [r"мес\w*"],
    "чел.": [r"чел\w*"], "т/год": [r"т/год", r"тонн\w*/год"], "г/с": [r"г/с"],
    "шт.": [r"шт\w*", r"ед\w*"], "м³/сут": [r"м3/сут\w*", r"м³/сут\w*", r"куб\.?\s*м/сут\w*"],
    "м": [r"м(?![²³2-9а-яё])"], "дБА": [r"дба", r"дб"],
}


def _value_rx(unit: str) -> re.Pattern:
    alts = "|".join(_UNIT_ALIASES.get(unit, [re.escape(unit)]))
    return re.compile(rf"({_NUM})\s*(?:{alts})\b", re.I)


def _norm_num(s: str) -> str:
    return re.sub(r"[  ]", "", (s or "")).replace(",", ".").rstrip(".")


def registry_path(project: str):
    return project_paths(project)["root"] / "registry.json"


def load_registry(project: str) -> dict:
    p = registry_path(project)
    if not p.exists():
        return {"project": project, "indicators": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"project": project, "indicators": {}}


def save_registry(project: str, reg: dict) -> None:
    import os
    p = registry_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    reg["updated_at"] = datetime.now().isoformat(timespec="seconds")
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _scan_text(text: str, ind: dict) -> list[str]:
    """Значения показателя в тексте чанка: синоним рядом с числом+единицей."""
    low = (text or "").lower()
    rx_val = _value_rx(ind["unit"])
    out: list[str] = []
    for syn in ind["syn"]:
        for m in re.finditer(syn, low):
            # окно после упоминания показателя: значение обычно правее. ОКНО
            # ОБРЫВАЕТСЯ НА ГРАНИЦЕ ПРЕДЛОЖЕНИЯ — иначе «выброс … т/год»
            # подхватывал число из соседней фразы про отходы (проверено).
            window = low[m.end(): m.end() + 160]
            cut = re.search(r"[.;]\s", window)
            if cut:
                window = window[: cut.start()]
            hit = rx_val.search(window)
            if hit:
                val = _norm_num(hit.group(1))
                lo, hi = ind.get("range", (None, None))
                if lo is not None:          # отсекаем неправдоподобное
                    try:
                        if not (lo <= float(val) <= hi):
                            continue
                    except ValueError:
                        continue
                out.append(val)
    return out


def extract_from_index(project: str, cfg=None, *, progress=None,
                       limit_chunks: int = 0) -> dict:
    """Пройти по чанкам проекта и собрать КАНДИДАТОВ значений с провенансом."""
    from ..config import load_config
    from ..index.vectorstore import VectorStore, collection_name
    cfg = cfg or load_config()
    store = VectorStore(cfg, dim=1024)
    client = store.client()
    coll = collection_name(project)
    reg = load_registry(project)
    inds = reg.setdefault("indicators", {})
    for i in INDICATORS:                       # кандидатов пересобираем заново
        rec = inds.setdefault(i["key"], {})
        rec["label"], rec["unit"] = i["label"], i["unit"]
        rec["candidates"] = []

    seen = 0
    offset = None
    while True:
        pts, offset = client.scroll(collection_name=coll, with_payload=True,
                                    with_vectors=False, limit=512, offset=offset)
        for p in pts:
            pl = p.payload or {}
            text = pl.get("text", "") or ""
            if not text:
                continue
            for ind in INDICATORS:
                for val in _scan_text(text, ind):
                    inds[ind["key"]]["candidates"].append({
                        "value": val, "unit": ind["unit"],
                        "file": pl.get("file", ""), "section": pl.get("section", ""),
                        "loc": pl.get("loc", ""), "chunk_id": str(p.id),
                        "snippet": text[:400],
                    })
            seen += 1
            if progress and seen % 500 == 0:
                progress(seen, f"Просмотрено фрагментов: {seen}")
        if offset is None or (limit_chunks and seen >= limit_chunks):
            break
    store.close()

    # схлопываем одинаковые значения, считаем встречаемость и расхождения
    for key, rec in inds.items():
        agg: dict[str, dict] = {}
        for c in rec.get("candidates", []):
            a = agg.setdefault(c["value"], {"value": c["value"], "unit": c["unit"],
                                            "count": 0, "sources": []})
            a["count"] += 1
            if len(a["sources"]) < 8:
                a["sources"].append({k: c[k] for k in ("file", "section", "loc")})
        # порядок вариантов: сначала из «профильного» раздела (ПОС владеет
        # сроками, ООС — выбросами и т.д.), затем по частоте встречаемости
        prefer = (_BY_KEY.get(key, {}).get("prefer") or [])

        def _rank(v: dict) -> tuple:
            secs = {s.get("section", "") for s in v.get("sources", [])}
            pri = min((prefer.index(s) for s in secs if s in prefer), default=99)
            return (pri, -v["count"])

        rec["variants"] = sorted(agg.values(), key=_rank)
        # ручной ввод и осознанный выбор варианта переживают пересбор, их
        # РАЗРЕШЁННЫЙ конфликт не «воскресает» (ревью: авто-сбор после каждой
        # индексации молча откатывал решение пользователя и снова ставил ⚠)
        if rec.get("source") in ("manual", "chosen"):
            rec["conflict"] = False
        else:
            rec["conflict"] = len(rec["variants"]) > 1
            if rec["variants"]:
                top = rec["variants"][0]
                rec["value"] = top["value"]
                rec["source"] = "auto"
                rec["provenance"] = top["sources"][0] if top["sources"] else {}
        rec.pop("candidates", None)             # в файле держим только сводку

    # СНИМКИ ЛИСТОВ-ИСТОЧНИКОВ сохраняем СЕЙЧАС: индексатор вызывает сбор до
    # удаления исходников, а без этого кнопка «Показать лист-источник» была
    # мертва в дефолтной конфигурации (ревью). ~13 PNG на проект — копейки.
    scans_dir = project_paths(project)["root"] / "scans"
    for key, rec in inds.items():
        prov = rec.get("provenance") or {}
        if not str(rec.get("value", "")).strip() or not prov.get("file"):
            continue
        try:
            png = render_source_page(project, prov.get("file", ""), prov.get("loc", ""))
            if png:
                scans_dir.mkdir(parents=True, exist_ok=True)
                (scans_dir / f"{key}.png").write_bytes(png)
                rec["scan"] = f"scans/{key}.png"
        except Exception:  # noqa: BLE001 — снимок вторичен, сбор важнее
            continue

    reg["scanned_chunks"] = seen
    save_registry(project, reg)
    return reg


def render_source_page(project: str, file: str, loc: str) -> bytes | None:
    """PNG-снимок ЛИСТА-ИСТОЧНИКА (ТЗ: «файл и скан/фото листа откуда данные»).

    Работает, пока исходник лежит в tmp_uploads проекта; после «Очистить
    временные файлы» вернёт None — тогда UI честно говорит, что скан недоступен."""
    import re as _re
    if not file:
        return None
    # provenance.file приходит из данных — берём только базовое имя (без ../)
    src = project_paths(project)["uploads"] / Path(str(file).replace("\\", "/")).name
    if not src.exists() or src.suffix.lower() != ".pdf":
        if src.exists() and src.suffix.lower() in (".jpg", ".jpeg", ".png"):
            try:
                return src.read_bytes()
            except OSError:
                return None
        if src.exists() and src.suffix.lower() in (".tif", ".tiff", ".bmp"):
            # браузер не показывает tiff/bmp — конвертируем в PNG (ревью)
            try:
                import io
                from PIL import Image
                buf = io.BytesIO()
                Image.open(str(src)).convert("RGB").save(buf, format="PNG")
                return buf.getvalue()
            except Exception:  # noqa: BLE001
                return None
        return None
    m = _re.search(r"стр\.\s*(\d+)", loc or "")
    if not m:
        return None
    page_no = int(m.group(1))
    try:
        import fitz
        doc = fitz.open(str(src))
        try:
            if not (1 <= page_no <= doc.page_count):
                return None
            pix = doc[page_no - 1].get_pixmap(dpi=120)
            return pix.tobytes("png")
        finally:
            doc.close()
    except Exception:  # noqa: BLE001
        return None


def set_value(project: str, key: str, value: str, *, unit: str = "",
              note: str = "") -> dict:
    """Ручной ввод/правка значения (ТЗ: «возможность внесения вручную»)."""
    reg = load_registry(project)
    rec = reg.setdefault("indicators", {}).setdefault(key, {})
    meta = _BY_KEY.get(key, {})
    rec.setdefault("label", meta.get("label", key))
    rec["unit"] = unit or rec.get("unit") or meta.get("unit", "")
    rec["value"] = _norm_num(value)
    rec["source"] = "manual"
    rec["note"] = note
    rec["provenance"] = {"file": "введено вручную", "section": "", "loc": ""}
    rec["conflict"] = False
    save_registry(project, reg)
    return reg


def choose_variant(project: str, key: str, value: str) -> dict:
    """Выбрать, какое из расходящихся значений считать верным."""
    reg = load_registry(project)
    rec = reg.setdefault("indicators", {}).setdefault(key, {})
    for v in rec.get("variants", []):
        if v["value"] == value:
            rec["value"] = value
            rec["source"] = "chosen"
            rec["provenance"] = (v.get("sources") or [{}])[0]
            rec["conflict"] = False
            break
    save_registry(project, reg)
    return reg


def conflicts(project: str) -> list[dict]:
    """Показатели, у которых разделы дают РАЗНЫЕ значения (кросс-сверка)."""
    reg = load_registry(project)
    out = []
    for key, rec in (reg.get("indicators") or {}).items():
        if rec.get("conflict") and len(rec.get("variants", [])) > 1:
            out.append({"key": key, "label": rec.get("label", key),
                        "unit": rec.get("unit", ""), "variants": rec["variants"]})
    return out


def summary(project: str) -> dict:
    reg = load_registry(project)
    inds = reg.get("indicators") or {}
    filled = [k for k, r in inds.items() if str(r.get("value", "")).strip()]
    return {"всего показателей": len(INDICATORS),
            "заполнено": len(filled),
            "не найдено": len(INDICATORS) - len(filled),
            "расхождений": len(conflicts(project)),
            "просмотрено фрагментов": reg.get("scanned_chunks", 0)}
