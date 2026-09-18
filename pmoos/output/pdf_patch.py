# -*- coding: utf-8 -*-
"""Правки ПОВЕРХ оригинального PDF тома (v0.55, вердикт dex-дебата 15.09.2026, п. 2b).

Том ООС приходит как PDF с рамками и штампами ГОСТ; конверсия в docx без
оформления — только рабочая копия. Чтобы форма листа не нарушалась, правки
кладутся прямо в копию PDF:
  * найденное «было» — жёлтая подсветка + выноска (sticky note) с текстом
    «стало» и номером замечания + закладка «★ ПРАВКА №N» (как делал
    эксперт-человек в эталонной сессии);
  * текстовый слой у таких PDF битый (+0x1D6), поэтому слова берутся через
    get_text("words") и декодируются (docx_writer.decode_garbled); поиск —
    по нормализованным словам со скользящим окном.
Замены текста внутри PDF (redact + insert) — отдельный шаг, здесь не делается:
форма важнее, а результат виден в аннотациях и в «Перечне изменений».
"""
from __future__ import annotations

import re
from pathlib import Path

from .docx_writer import decode_garbled

_WORD_RX = re.compile(r"[а-яёa-z0-9]+")


def _norm_tokens(s: str) -> list[str]:
    s = (s or "").lower().replace("ё", "е").replace("­", "")
    s = re.sub(r"[‐-―−]", "-", s)
    return _WORD_RX.findall(s)


def _page_tokens(page) -> list[tuple[list[str], tuple[float, float, float, float]]]:
    """[(токены слова, bbox)] в порядке чтения; слово декодировано."""
    out = []
    for w in page.get_text("words"):
        x0, y0, x1, y1, text = w[0], w[1], w[2], w[3], w[4]
        toks = _norm_tokens(decode_garbled(text))
        if toks:
            out.append((toks, (x0, y0, x1, y1)))
    return out


def page_tokens_cache(doc) -> list[list[tuple[list[str], tuple[float, float, float, float]]]]:
    """Слова всех страниц один раз (408 страниц ≈ 40 с; поиск 75 цитат
    по готовому кэшу — секунды)."""
    return [_page_tokens(doc[i]) for i in range(doc.page_count)]


def find_quote(doc, was: str, *, max_miss: float = 0.10, pages: list[int] | None = None,
               cache: list | None = None):
    """Где в PDF встречается цитата «было»: (номер страницы 0-based, [bbox…], доля совпадений).
    Предпочитается ТОЧНОЕ совпадение; при нескольких неточных — лучшее."""
    q = _norm_tokens(was)
    if len(q) < 3:
        return None
    q_digits = {t for t in q if t.isdigit()}
    rng = list(pages) if pages is not None else list(range(doc.page_count))
    flats: dict[int, tuple[list, list]] = {}

    def _flat(pno: int):
        if pno not in flats:
            words = cache[pno] if cache is not None else _page_tokens(doc[pno])
            fl: list[tuple[str, int]] = []
            for wi, (toks, _) in enumerate(words):
                for t in toks:
                    fl.append((t, wi))
            flats[pno] = (words, fl)
        return flats[pno]

    n = len(q)

    def _match_at(flat: list, start: int) -> tuple[int, set[int], set[str]]:
        """Сопоставить цитату с токенами страницы от позиции start.
        Слово, разорванное переносом («представле» + «ны»), склеивается;
        допускается пропуск одного токена цитаты. → (совпало, индексы слов, токены)."""
        matched, qi, k, gaps = 0, 0, start, 0
        used: set[int] = set()
        seen: set[str] = set()
        limit = min(len(flat), start + n + 3)
        while qi < n and k < limit:
            t, wi = flat[k]
            qt = q[qi]
            if t == qt:
                matched += 1; used.add(wi); seen.add(t); qi += 1; k += 1
            elif k + 1 < len(flat) and qt.startswith(t) and t + flat[k + 1][0] == qt:
                matched += 1; used.add(wi); used.add(flat[k + 1][1]); seen.add(qt); qi += 1; k += 2
            elif qi + 1 < n and t == q[qi + 1]:
                matched += 1; used.add(wi); seen.add(t); qi += 2; k += 1; gaps += 1
            elif t and qt.startswith(t) and k + 2 < len(flat) and t + flat[k + 1][0] + flat[k + 2][0] == qt:
                matched += 1; used.update((wi, flat[k + 1][1], flat[k + 2][1])); seen.add(qt); qi += 1; k += 3
            else:
                k += 1; gaps += 1
                if k - start > n + 3:
                    break
        return matched, used, seen, gaps

    # 1) ТОЧНОЕ совпадение (с учётом переносов) — приоритет: иначе похожая
    #    фраза с другими номерами таблиц на соседней странице выигрывала
    best = None
    for pno in rng:
        words, flat = _flat(pno)
        if len(flat) < n * 0.7:
            continue
        for start in range(0, max(1, len(flat) - int(n * 0.7))):
            if flat[start][0] != q[0] and not q[0].startswith(flat[start][0]):
                continue
            matched, used, seen, gaps = _match_at(flat, start)
            if matched == n and gaps == 0:
                return (pno, [words[i][1] for i in sorted(used)], 1.0)
            score = matched / n
            if gaps <= 2 and score >= 1 - max_miss and q_digits <= seen and (best is None or score > best[2]):
                best = (pno, [words[i][1] for i in sorted(used)], round(score - 0.01 * gaps, 2))
    # 2) точного нет — лучшее неточное, у которого совпали ВСЕ числа цитаты
    return best


_LOC_PAGE_RX = re.compile(r"стр\.?\s*(\d{1,4})", re.I)
_LOC_TABLE_RX = re.compile(r"табл(?:ица|ицу|ице|ицы|\.)?\s*№?\s*(\d+(?:\.\d+)+|\d+)", re.I)
_LOC_ITEM_RX = re.compile(r"(?:п\.|пункт[а-я]*|раздел[а-я]*|подраздел[а-я]*)\s*(\d+(?:\.\d+){0,4})", re.I)


def page_lines(doc) -> list[list[tuple[str, tuple[float, float, float, float]]]]:
    """Строки всех страниц: [(декодированный текст строки, bbox)] — для поиска
    заголовков пунктов и таблиц (правки-ДОБАВЛЕНИЯ без «было»)."""
    out = []
    for i in range(doc.page_count):
        rows: dict[tuple[int, int], list] = {}
        for w in doc[i].get_text("words"):
            rows.setdefault((w[5], w[6]), []).append(w)
        lines = []
        for key in sorted(rows):
            ws = sorted(rows[key], key=lambda w: w[0])
            text = decode_garbled(" ".join(w[4] for w in ws)).strip()
            if text:
                lines.append((text, (min(w[0] for w in ws), min(w[1] for w in ws),
                                     max(w[2] for w in ws), max(w[3] for w in ws))))
        out.append(lines)
    return out


def find_location(doc, location: str, lines: list | None = None):
    """Страница и bbox места правки по полю «где править»: заголовок таблицы →
    заголовок пункта → «стр. N». Строки оглавления (хвост — номер страницы или
    отточие) пропускаются. → (pno, bbox, как нашли) либо None."""
    loc = location or ""
    lines = lines if lines is not None else page_lines(doc)
    toc_pages = {pno for pno, pl in enumerate(lines)
                 if sum(1 for t, _ in pl if re.search(r"\.{5,}|…{3,}", t)) >= 6}
    targets: list[tuple[str, re.Pattern]] = []
    for m in _LOC_TABLE_RX.finditer(loc):
        targets.append((f"таблица {m.group(1)}",
                        re.compile(r"^\s*табл(?:ица|\.)\s*№?\s*" + re.escape(m.group(1)) + r"(?!\d|\.\d)", re.I)))
    for m in _LOC_ITEM_RX.finditer(loc):
        num = m.group(1).rstrip(".")
        if "." not in num and not re.search(r"раздел", m.group(0), re.I):
            continue                      # «п. 5» слишком общо — совпадёт с чем угодно
        targets.append((f"п. {num}", re.compile(r"^\s*" + re.escape(num) + r"\.?\s+[А-ЯЁA-Z«\"]")))
    for label, rx in targets:
        for pno, pl in enumerate(lines):
            if pno in toc_pages:
                continue
            for text, bbox in pl:
                if not rx.search(text):
                    continue
                if re.search(r"(\.{3,}|…)\s*\d*\s*$", text) or re.search(r"\s\d{1,3}\s*$", text):
                    continue              # строка оглавления
                return pno, bbox, label
    m = _LOC_PAGE_RX.search(loc)
    if m:
        pno = int(m.group(1)) - 1
        if 0 <= pno < doc.page_count:
            pl = lines[pno]
            return pno, (pl[0][1] if pl else (40.0, 40.0, 60.0, 60.0)), f"стр. {pno + 1}"
    return None


def _merge_line_rects(rects: list) -> list[tuple[float, float, float, float]]:
    """Прямоугольники слов → прямоугольники строк (слова одной строки склеены)."""
    out: list[list[float]] = []
    for r in sorted(rects, key=lambda r: (round(r[1], 0), r[0])):
        if out and abs(out[-1][1] - r[1]) < 3 and r[0] - out[-1][2] < 25:
            out[-1][2] = max(out[-1][2], r[2])
            out[-1][1] = min(out[-1][1], r[1])
            out[-1][3] = max(out[-1][3], r[3])
        else:
            out.append([r[0], r[1], r[2], r[3]])
    return [tuple(x) for x in out]


def annotate_pdf(src_pdf: str | Path, items: list[dict], out_pdf: str | Path) -> dict:
    """items: [{number, edit_was, edit_shall, edit_location}] → копия PDF с
    подсветкой/выносками/закладками. Возвращает отчёт по каждому пункту."""
    import fitz  # PyMuPDF
    src_pdf, out_pdf = Path(src_pdf), Path(out_pdf)
    doc = fitz.open(str(src_pdf))
    report: list[dict] = []
    toc = doc.get_toc() or []
    placed = at_heading = 0
    lines = None
    loose: list[tuple] = []
    try:
        cache = page_tokens_cache(doc)
        for it in items:
            num = str(it.get("number", "?"))
            was = (it.get("edit_was") or "").strip()
            shall = (it.get("edit_shall") or "").strip()
            if not shall:
                report.append({"number": num, "status": "пропуск", "note": "нет текста «стало»"})
                continue
            hit = find_quote(doc, was, cache=cache) if was else None
            if not hit:
                # ДОБАВЛЕНИЕ без «было» либо цитата не найдена: выноска у заголовка
                # пункта/таблицы из «где править»; нет и его — в сводку на 1-й странице
                # (тестировщик №3: в PDF попадало 28 правок из 91)
                if lines is None:
                    lines = page_lines(doc)
                # место из ответа не сходится с замечанием — у пункта не ставим
                where = None if it.get("location_mismatch") else \
                    find_location(doc, it.get("edit_location") or "", lines)
                why = "цитата «было» в PDF не найдена" if was else "добавление (без «было»)"
                if it.get("location_mismatch"):
                    why += "; место в ответе не соответствует замечанию"
                if where:
                    pno, bbox, label = where
                    r0 = fitz.Rect(*bbox)
                    note = doc[pno].add_text_annot(
                        fitz.Point(max(r0.x0 - 18, 5), r0.y0),
                        f"ПРАВКА ПО ЗАМЕЧАНИЮ №{num} — РАЗМЕСТИТЬ В ЭТОМ ПУНКТЕ\n"
                        f"ГДЕ: {it.get('edit_location') or '—'}\n"
                        + (f"БЫЛО (не найдено дословно): {was[:400]}\n" if was else "")
                        + f"СТАЛО / ДОБАВИТЬ: {shall[:1200]}", icon="Insert")
                    note.set_info(title=f"STR.RAG №{num}")
                    note.update()
                    toc.append([1, f"☆ ПРАВКА №{num} (у {label}) — стр. {pno + 1}", pno + 1])
                    at_heading += 1
                    report.append({"number": num, "status": "выноска у пункта", "page": pno + 1,
                                   "note": f"{why}; выноска у «{label}», стр. {pno + 1}"})
                else:
                    loose.append((num, it.get("edit_location") or "—", was, shall, why))
                    report.append({"number": num, "status": "сводка на стр. 1", "page": 1,
                                   "note": f"{why}; место в PDF не определено — см. сводку на первой странице"})
                continue
            pno, rects, score = hit
            page = doc[pno]
            try:
                # одна подсветка на правку: слова склеены в строки (тестировщик №4:
                # ~1370 отдельных подсветок «по слову» в трёх томах)
                quads = [fitz.Rect(*r).quad for r in _merge_line_rects(rects)]
                a = page.add_highlight_annot(quads=quads)
                a.set_info(title=f"STR.RAG №{num}")
                a.update()
            except Exception:  # noqa: BLE001
                pass
            r0 = fitz.Rect(*rects[0])
            note = page.add_text_annot(fitz.Point(max(r0.x0 - 18, 5), r0.y0),
                                       f"ПРАВКА ПО ЗАМЕЧАНИЮ №{num}\n"
                                       f"ГДЕ: {it.get('edit_location') or '—'}\n"
                                       f"БЫЛО: {was[:400]}\nСТАЛО: {shall[:1200]}",
                                       icon="Comment")
            note.set_info(title=f"STR.RAG №{num}")
            note.update()
            toc.append([1, f"★ ПРАВКА №{num} — стр. {pno + 1}", pno + 1])
            placed += 1
            report.append({"number": num, "status": "аннотация", "page": pno + 1,
                           "score": score, "note": f"подсветка «было» + выноска со «стало», стр. {pno + 1}"})
        # сводка правок без определённого места — выноски столбиком на 1-й странице
        for k, (num, loc, was, shall, why_l) in enumerate(loose):
            note = doc[0].add_text_annot(
                fitz.Point(8, 30 + 22 * (k % 34)),
                f"ПРАВКА ПО ЗАМЕЧАНИЮ №{num} — МЕСТО ОПРЕДЕЛИТЬ ВРУЧНУЮ ({why_l})\nГДЕ: {loc}\n"
                + (f"БЫЛО: {was[:400]}\n" if was else "") + f"СТАЛО / ДОБАВИТЬ: {shall[:1200]}",
                icon="Help")
            note.set_info(title=f"STR.RAG №{num}")
            note.update()
        if loose:
            toc.append([1, f"☆ ПРАВКИ БЕЗ МЕСТА ({len(loose)}) — стр. 1", 1])
        if placed or at_heading or loose:
            toc.sort(key=lambda t: (t[2], t[1]))
            if toc and toc[0][0] != 1:      # set_toc: первый пункт обязан быть уровня 1
                toc[0][0] = 1
            doc.set_toc(toc)
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(out_pdf), garbage=1, deflate=True)
    finally:
        doc.close()
    return {"output": str(out_pdf), "placed": placed, "at_heading": at_heading, "loose": len(loose),
            "total": len(items), "rows": report}
