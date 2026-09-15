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


def annotate_pdf(src_pdf: str | Path, items: list[dict], out_pdf: str | Path) -> dict:
    """items: [{number, edit_was, edit_shall, edit_location}] → копия PDF с
    подсветкой/выносками/закладками. Возвращает отчёт по каждому пункту."""
    import fitz  # PyMuPDF
    src_pdf, out_pdf = Path(src_pdf), Path(out_pdf)
    doc = fitz.open(str(src_pdf))
    report: list[dict] = []
    toc = doc.get_toc() or []
    placed = 0
    try:
        cache = page_tokens_cache(doc)
        for it in items:
            num = str(it.get("number", "?"))
            was = (it.get("edit_was") or "").strip()
            shall = (it.get("edit_shall") or "").strip()
            if not was or not shall:
                report.append({"number": num, "status": "пропуск", "note": "нет «было» или «стало»"})
                continue
            hit = find_quote(doc, was, cache=cache)
            if not hit:
                report.append({"number": num, "status": "не найдено",
                               "note": "цитата «было» в PDF не найдена (проверьте дословность)"})
                continue
            pno, rects, score = hit
            page = doc[pno]
            for r in rects:
                try:
                    a = page.add_highlight_annot(fitz.Rect(*r))
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
        if placed:
            toc.sort(key=lambda t: (t[2], t[1]))
            doc.set_toc(toc)
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(out_pdf), garbage=1, deflate=True)
    finally:
        doc.close()
    return {"output": str(out_pdf), "placed": placed, "total": len(items), "rows": report}
