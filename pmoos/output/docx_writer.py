"""МОДУЛЬ 5 (часть 1): формирование откорректированного раздела ПМООС в .docx.

Архитектурное замечание (важно):
  По требованию пользователя исходные файлы проекта НЕ хранятся приложением
  (см. fix #9 — храним только имя проекта и чанки/токены в RAG-базе). Поэтому
  «откорректированный ПМООС» формируется как профессиональный документ-носитель
  корректировок: для каждого принятого ответа выводится конкретная правка в
  раздел ПМООС со ссылкой на источник (раздел/файл/страница). Если пользователь
  передаёт путь к исходному файлу ПМООС (original_oos_path), его текст
  извлекается и добавляется отдельным приложением для удобства сверки —
  непосредственного слепого переписывания произвольного документа не делаем,
  чтобы не повредить нормоконтроль.

Используется python-docx (работает на машине пользователя без Node.js).
Применяются принципы оформления из docx-skill: шрифт Arial, явные ширины
колонок таблиц (DXA), нумерация средствами Word, без юникод-«буллетов».
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH

from ..paths import project_paths
from .common import (
    accepted_answers, final_answer_text, source_ref, set_default_font,
    add_title, add_heading,
)


def _add_intro(doc: Document, project: str, object_type: str, n_corr: int, accepted_only: bool) -> None:
    from .. import VERSION
    p = doc.add_paragraph()
    run = p.add_run(
        f"Документ сформирован автоматически системой {VERSION} "
        f"{datetime.now().strftime('%d.%m.%Y %H:%M')}. "
        f"Проект: «{project}». Тип объекта: {object_type}. "
    )
    run.font.size = Pt(10)
    p2 = doc.add_paragraph()
    note = (
        f"Включено корректировок: {n_corr} (только принятые пользователем)."
        if accepted_only else
        f"Включено корректировок: {n_corr} (ВНИМАНИЕ: показаны предлагаемые ответы — "
        f"ни один пункт ещё не принят пользователем в Модуле 4)."
    )
    r2 = p2.add_run(note)
    r2.font.size = Pt(10)
    r2.italic = True
    if not accepted_only:
        r2.font.color.rgb = RGBColor(0xB0, 0x00, 0x00)


def _add_corrections(doc: Document, answers: list[dict]) -> None:
    add_heading(doc, "1. Корректировки раздела ПМООС по замечаниям экспертизы", level=1)
    if not answers:
        doc.add_paragraph("Принятых корректировок нет.")
        return
    for a in answers:
        num = a.get("number", "?")
        add_heading(doc, f"Замечание №{num}", level=2)

        pr = doc.add_paragraph()
        pr.add_run("Замечание эксперта: ").bold = True
        pr.add_run(a.get("remark", "") or "—")

        corr = (a.get("user_answer") or a.get("correction") or "").strip()
        pc = doc.add_paragraph()
        pc.add_run("Вносимая правка в ПМООС: ").bold = True
        pc.add_run(corr or "—")

        ans = final_answer_text(a)
        if ans:
            pa = doc.add_paragraph()
            pa.add_run("Ответ для экспертизы: ").bold = True
            pa.add_run(ans)

        src = source_ref(a)
        ps = doc.add_paragraph()
        rs = ps.add_run(f"Источник: {src}")
        rs.italic = True
        rs.font.size = Pt(9)
        rs.font.color.rgb = RGBColor(0x55, 0x55, 0x55)

        md = (a.get("missing_data") or "").strip()
        if md:
            pm = doc.add_paragraph()
            rm = pm.add_run(f"Требуется дополнить данными: {md}")
            rm.font.size = Pt(9)
            rm.font.color.rgb = RGBColor(0xB0, 0x60, 0x00)


def _add_original_appendix(doc: Document, original_oos_path: str | Path, cfg) -> None:
    from ..ingest.loaders import extract_file
    try:
        pages = extract_file(Path(original_oos_path), ocr=False)
    except Exception as exc:  # noqa: BLE001
        doc.add_paragraph(f"(Не удалось прочитать исходный ПМООС: {exc})")
        return
    doc.add_page_break()
    add_heading(doc, "Приложение А. Исходный текст раздела ПМООС (для сверки)", level=1)
    note = doc.add_paragraph()
    rn = note.add_run(
        "Ниже приведён извлечённый текст исходного (неоткорректированного) раздела. "
        "Используйте его как основу: примените к нему правки из раздела 1."
    )
    rn.italic = True
    rn.font.size = Pt(9)
    for pg in pages:
        txt = (pg.get("text") or "").strip()
        if not txt:
            continue
        loc = pg.get("loc", "")
        if loc:
            h = doc.add_paragraph()
            rh = h.add_run(str(loc))
            rh.bold = True
            rh.font.size = Pt(9)
            rh.font.color.rgb = RGBColor(0x88, 0x88, 0x88)
        for para in txt.split("\n"):
            para = para.strip()
            if para:
                doc.add_paragraph(para)


def build_corrected_oos_docx(project: str, *, original_oos_path: str | Path | None = None,
                             cfg=None, out_path: str | Path | None = None) -> Path:
    """Сформировать .docx с откорректированным разделом ПМООС.

    Возвращает путь к созданному файлу (по умолчанию в out-папке проекта).
    """
    from ..config import load_config
    cfg = cfg or load_config()
    data = _load_answers(project)
    object_type = data.get("object_type") or cfg.get("object_type", "площадной")

    answers, accepted_only = accepted_answers(data)

    doc = Document()
    set_default_font(doc, "Arial", 11)
    add_title(doc, "ОТКОРРЕКТИРОВАННЫЙ РАЗДЕЛ ПМООС/ООС")
    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    rsub = sub.add_run("Перечень мероприятий по охране окружающей среды")
    rsub.font.size = Pt(12)
    rsub.bold = True

    _add_intro(doc, project, object_type, len(answers), accepted_only)
    _add_corrections(doc, answers)

    if original_oos_path:
        _add_original_appendix(doc, original_oos_path, cfg)

    out_dir = project_paths(project)["out"]
    out_dir.mkdir(parents=True, exist_ok=True)
    out = Path(out_path) if out_path else out_dir / f"ПМООС_откорректированный_{project}.docx"
    doc.save(str(out))
    return out


def _load_answers(project: str) -> dict[str, Any]:
    from ..pipeline.block1_answers import load_answers
    return load_answers(project)


# ───────────── №10-11..14: правки ПРЯМО в исходных томах (жёлтым) ─────────────

def _anchor_token(text: str) -> str | None:
    """Маркер места из текста правки: «табл. 4.1», «т. 8.3», «раздел 5», «п. 2.3».

    Падежи учтены («в разделУ/пунктЕ/таблицАХ…») — иначе правка молча уходила
    «в конец» при обычной канцелярской формулировке замечания."""
    import re as _re
    # (?<![а-яёa-z]) — граница слова СЛЕВА: без неё «этап. 5» матчился на «п.»,
    # а «результат. 6» — на «т.» (находка аудита: якорь вставал в чужое место)
    m = _re.search(r"(?<![а-яёa-z])(?:табл(?:иц[аыеуах]{1,2}|\.)?|т\.|разд(?:ел[аеуы]?|\.)?|"
                   r"п(?:ункт[аеуы]?|\.)\.?)"
                   r"\s*№?\s*([\d][\d.]*)", (text or "").lower())
    return m.group(1).rstrip(".") if m else None


def _anchor_re(tok: str):
    """Регэксп поиска якоря с границами по цифрам: «4.1» НЕ должен находиться
    внутри «14.1», «4.12» или даты «04.11.2025» (иначе правка вставала не туда)."""
    import re as _re
    return _re.compile(r"(?<![\d.])" + _re.escape(tok) + r"(?![\d])")


def _find_anchor_paragraph(ptexts, tok: str | None):
    """Первый абзац, содержащий якорный номер (общая логика preview и записи).

    ptexts: список (paragraph|None, lower_text). Возвращает paragraph или None."""
    if not tok:
        return None
    rx = _anchor_re(tok)
    for p, lt in ptexts:
        if rx.search(lt):
            return p
    return None


def _iter_all_paragraphs(container, _seen=None):
    """Все абзацы документа: тело + ячейки таблиц (рекурсивно, включая вложенные).

    python-docx `document.paragraphs` НЕ включает абзацы внутри таблиц. В реальных
    томах ООС нумерованные таблицы/пункты («табл. 4.1», «п. 5.2») почти всегда
    лежат в таблицах — без этого обхода якорь не находится и правки уходят «в конец».
    Объединённые (merged) ячейки дедуплицируются по XML-элементу — иначе один и
    тот же абзац отдавался несколько раз."""
    if _seen is None:
        _seen = set()
    for p in container.paragraphs:
        yield p
    for tbl in getattr(container, "tables", []):
        for row in tbl.rows:
            for cell in row.cells:
                tc_id = id(cell._tc)
                if tc_id in _seen:
                    continue
                _seen.add(tc_id)
                yield from _iter_all_paragraphs(cell, _seen)


def _insert_paragraph_after(par, runs):
    """Вставляет новый абзац СРАЗУ ПОСЛЕ par. runs = [(text, bold, yellow)]."""
    from docx.text.paragraph import Paragraph
    from docx.oxml.ns import qn
    from docx.enum.text import WD_COLOR_INDEX
    new_p = par._p.makeelement(qn("w:p"), {})
    par._p.addnext(new_p)
    np = Paragraph(new_p, par._parent)
    for t, bold, hl in runs:
        r = np.add_run(t)
        r.bold = bold
        if hl:
            r.font.highlight_color = WD_COLOR_INDEX.YELLOW
    return np


def _sub_bounded(needle: str, hay: str) -> bool:
    """Подстрока с ЦИФРОВОЙ границей справа: «том 6» НЕ находится в «том 6.1»
    (иначе правка для тома 6.1 вставала и в том 6 — находка аудита; тома у
    пользователя реально нумеруются 6/6.1/6.2)."""
    import re as _re
    # граница: не цифра и не «.цифра» («том 6» ≠ «том 6.1»), но точка
    # РАСШИРЕНИЯ допустима («том 6.3» ∈ «том 6.3.docx») — иначе (05.08) ни один
    # ответ не матчился с томом и все 75 правок уезжали в первый том
    return bool(needle) and _re.search(_re.escape(needle) + r"(?!\.?\d)", hay) is not None


def _volume_tokens(text: str) -> set[str]:
    """Номера томов, названные в тексте: «Том 6.1», «том 6.2,», «тома 6.1–6.3»
    → {'6.1','6.2'}. Диапазон «6.1–6.3» раскрывается по последней цифре."""
    import re as _re
    out: set[str] = set()
    low = (text or "").lower().replace("ё", "е")
    num = r"\d+(?:\.\d+)+"
    # после слова «том/тома» — ЦЕПОЧКА номеров: «6.1, 6.2 и 6.3», «6.1–6.3»
    # (ревью: «тома 6.1 и 6.2» давало только 6.1)
    for m in _re.finditer(r"том[аеу]?\s*№?\s*(" + num + r"(?:\s*(?:,|;|и|[-–—])\s*" + num + r")*)", low):
        chain = m.group(1)
        parts = _re.split(r"\s*(?:,|;|и)\s*", chain)
        for part in parts:
            rng = _re.split(r"\s*[-–—]\s*", part)
            if len(rng) == 2:
                a, b = rng
                pa, pb = a.rsplit(".", 1), b.rsplit(".", 1)
                if pa[0] == pb[0] and pa[1].isdigit() and pb[1].isdigit() and int(pa[1]) <= int(pb[1]):
                    for k in range(int(pa[1]), int(pb[1]) + 1):
                        out.add(f"{pa[0]}.{k}")
                    continue
            for tok in _re.findall(num, part):
                out.add(tok)
    return out


def _src_volume_token(src: Path) -> str:
    """Номер тома из имени файла: «Раздел ПД №6_ООС_том 6.1.docx» → '6.1'."""
    import re as _re
    m = _re.search(r"том[аеу]?\s*№?\s*(\d+(?:\.\d+)+)", src.stem.lower())
    if m:
        return m.group(1)
    m = _re.search(r"(\d+\.\d+(?:\.\d+)*)", src.stem)
    return m.group(1) if m else ""


def _match_volume(a: dict, src: Path) -> bool:
    """Относится ли принятый ответ к данному тому.

    Смотрим НЕ ТОЛЬКО служебное поле «Том ООС» (оно часто пусто/неверно), но и
    «где править» и текст замечания: «Том 6.2, п. 4.2.3» → том 6.2. Реальный
    случай (05.09): полтора десятка правок «Том 6.2/6.3» ложились в том 6.1."""
    tok = _src_volume_token(src)
    # 0) v0.55: тома-адресаты, определённые из текста замечания при ответе
    tv = a.get("target_volumes") or []
    if tv and tok:
        return tok in tv
    # Приоритет: 1) «Том X.Y» в «где править» (ИИ пишет адрес правки);
    # 2) служебное поле «Том ООС»; 3) только если оба пусты — тома из текста
    # замечания (ревью: в замечании часто упомянут ЧУЖОЙ том — «см. том 5.1
    # ПОС» — и он не должен перебивать верное поле)
    named = _volume_tokens(a.get("edit_location") or "")
    if named and tok:
        return tok in named
    v = (a.get("oos_volume") or "").lower().strip()
    n, stem = src.name.lower(), src.stem.lower()
    if not v:
        named = _volume_tokens(a.get("remark") or "")
        return bool(tok) and tok in named
    if _sub_bounded(v, n) or _sub_bounded(n, v) or _sub_bounded(stem, v):
        return True
    vp = Path(v)
    # stem берём ТОЛЬКО если v — имя файла с настоящим расширением
    # (иначе Path("том 6.1").stem == "том 6" и правка утекает в чужой том)
    if vp.suffix.lower() in (".docx", ".doc", ".pdf"):
        return _sub_bounded(vp.stem.lower(), n)
    return False


# ─────────────── v0.45: настоящая корректировка тома ───────────────
# Жалоба пользователя (05.08): «текст замечания просто напечатан поверх ООС —
# нужно в ООС находить, где исправлять, что на что, и делать откорректированный
# том». Диагностика на реальных томах ОПОЧКИ показала ТРИ причины:
#  1) исходные .docx — конверсия из PDF с ИСПОРЧЕННОЙ кодировкой шрифта: в XML
#     «Ɂɚɤɚɡɱɢɤ» вместо «Заказчик» (единое смещение +0x1D6 + 3 спецсимвола);
#     поиск по нормальному тексту не находил НИЧЕГО;
#  2) 33 % текста лежит в текстовых рамках (w:txbxContent) — python-docx их
#     не обходит;
#  3) каждая строка PDF — отдельный абзац (медиана 20 символов, слова разорваны
#     переносами «сель скохозяйственного») — сравнение с одним абзацем бессмысленно.
# Решение: декодер кодировки, обход всех w:p (включая рамки), поиск места по
# ОКНУ соседних строк через символьные n-граммы без пробелов (переносам всё
# равно), замена группы строк на «стало» СТАНДАРТНЫМ шрифтом (в кастомном
# шрифте тома обычная кириллица показалась бы кракозябрами), markdown-таблица
# → настоящая таблица docx, не найденное — компактно в конец без текста ответа.
# Один план для предпросмотра и записи.

_GARBLE_SHIFT = 0x1D6
_GARBLE_EXTRA = {"ʋ": "№", "ɺ": "ё", "ʌ": "/", "Ɫ": "Л"}


def decode_garbled(text: str) -> str:
    """Восстановить кириллицу из «ɡɚɤɚɡɱɢɤ»-кодировки PDF→DOCX конверсий."""
    out = []
    for ch in text or "":
        o = ord(ch)
        if ch in _GARBLE_EXTRA:
            out.append(_GARBLE_EXTRA[ch])
        elif 0x0230 <= o <= 0x02AF:
            d = o + _GARBLE_SHIFT
            out.append(chr(d) if 0x0410 <= d <= 0x044F else ch)
        else:
            out.append(ch)
    return "".join(out)


def garble_ratio(text: str) -> float:
    """Доля «испорченных» символов — признак конверсии из PDF."""
    if not text:
        return 0.0
    bad = sum(1 for ch in text if 0x0230 <= ord(ch) <= 0x02AF)
    return bad / len(text)


def _all_paragraphs(doc):
    """ВСЕ абзацы документа в документном порядке — тело, таблицы, текстовые
    рамки (w:txbxContent): python-docx `paragraphs` рамок не видит, а в
    PDF-конверсиях там треть текста."""
    from docx.oxml.ns import qn
    from docx.text.paragraph import Paragraph
    # БЕЗ дедупа по id(el): lxml отдаёт временные прокси, id переиспользуется —
    # такой «seen» молча пропускал абзацы (05.08: метки «терялись», 2 из 15).
    # body.iter и так выдаёт каждый элемент ровно один раз.
    for el in doc.element.body.iter(qn("w:p")):
        yield Paragraph(el, doc)


def _norm(s: str) -> str:
    """Для сравнения: декод, нижний регистр, ё→е, без пробелов и пунктуации —
    переносы строк PDF («свя зи») перестают мешать."""
    import re as _re
    s = decode_garbled(s or "").lower().replace("ё", "е")
    return _re.sub(r"[^а-яa-z0-9]+", "", s)


def _grams(s: str, n: int = 5) -> set[str]:
    return {s[i:i + n] for i in range(len(s) - n + 1)} if len(s) >= n else set()


_STOP_RU = {
    "в", "на", "и", "с", "по", "для", "от", "до", "из", "при", "не", "что", "как",
    "или", "а", "о", "об", "у", "к", "за", "же", "то", "это", "его", "их", "бы",
    "был", "была", "были", "было", "быть", "также", "том", "тома", "раздел",
    "разделе", "пункт", "пункте", "указано", "указан", "указана", "указаны",
    "данные", "данных", "проект", "проекта", "проектом", "объект", "объекта",
    "объекте", "настоящий", "настоящем", "рассматриваемого", "рассматриваемый",
    "согласно", "соответствии", "требований", "требованиями", "приведены",
    "приведен", "приведена", "представлены", "представлен", "представлена",
    "отсутствуют", "отсутствует", "имеются", "имеется", "необходимо", "следует",
    # юридические слова-паразиты: уводили правки в список нормативов
    # («Постановления Правительства Российской Федерации», 05.09)
    "постановление", "постановления", "постановлением", "правительства",
    "правительство", "российской", "федерации", "федеральный", "федерального",
    "закона", "закон", "приказ", "приказа", "приказом", "требования", "статьи",
    # общие слова названий глав ООС: без них «МЕРОПРИЯТИЯ ПО ОХРАНЕ ОКРУЖАЮЩЕЙ
    # СРЕДЫ» совпадала с любой темой и перебивала профильный раздел (ревью)
    "окружающей", "окружающую", "окружающая", "среды", "среду", "среда",
    "охрана", "природной", "природную", "природная", "перечень", "перечня",
    # каркасные слова заголовков ООС — тему задаёт ОБЪЕКТ воздействия
    # («…АКУСТИЧЕСКИХ ПОЛЕЙ», «…НА РАСТИТЕЛЬНЫЙ МИР»), а не эти слова
    "мероприятия", "мероприятий", "мероприятиям", "воздействие", "воздействия",
    "воздействию", "воздействий", "оценка", "оценки", "оценке", "оценку",
    "пункта", "пункту", "пунктом", "уточнить", "указать", "представить",
    "привести", "дополнить", "обосновать", "замечание", "замечания", "экспертизы",
}


_STOP_PREF: set[str] = set()


def _sig_words(text: str) -> set[str]:
    """Значимые слова → префиксы 5 букв (грубый стемминг). Стоп-слова
    сверяются ТОЖЕ по префиксу: иначе «мероприятиям»/«охраной» проходили и
    общая глава «МЕРОПРИЯТИЯ ПО ОХРАНЕ ОКРУЖАЮЩЕЙ СРЕДЫ» ловила любую тему."""
    import re as _re
    if not _STOP_PREF:
        _STOP_PREF.update(w[:5] for w in _STOP_RU if len(w) >= 5)
    out: set[str] = set()
    for w in _re.findall(r"[а-яёa-z0-9]+", decode_garbled(text or "").lower()):
        if len(w) < 5 or w in _STOP_RU or w[:5] in _STOP_PREF:
            continue
        out.add(w[:5])
    return out


def _loc_hints(location: str) -> list[tuple[str, str]]:
    """[(вид, номер)] из «где править»: вид — "table" или "item". «Таблица 3.6»
    ищется ТОЛЬКО как подпись таблицы и никогда не понижается до «п. 3.6»
    (тестировщик №4: правка к таблице 3.6 вставала в п. 3.6 «шум»)."""
    import re as _re
    out: list[tuple[str, str]] = []
    for m in _re.finditer(r"(п(?:ункт[аеуы]?|\.)|табл\w*\.?|разд\w*\.?)\s*№?\s*(\d+(?:\.\d+)*)",
                          (location or "").lower()):
        kind = "table" if m.group(1).startswith("табл") else "item"
        out.append((kind, m.group(2).rstrip(".")))
    multi = [h for h in out if "." in h[1]]
    single = [h for h in out if "." not in h[1]]
    return list(dict.fromkeys(multi + single))


def _is_md_table(text: str) -> bool:
    lines = [l for l in (text or "").splitlines() if l.strip()]
    return len(lines) >= 2 and sum(1 for l in lines if l.count("|") >= 2) >= 2


def _md_table_rows(text: str) -> list[list[str]]:
    import re as _re
    rows = []
    for l in (text or "").splitlines():
        if l.count("|") < 2:
            continue
        cells = [c.strip() for c in l.strip().strip("|").split("|")]
        if all(_re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        rows.append(cells)
    return rows


class _Index:
    """Индекс абзацев тома для быстрого нечёткого поиска."""

    def __init__(self, doc):
        from docx.oxml.ns import qn as _qn
        self.pars = list(_all_paragraphs(doc))
        self.text = [(p.text or "") for p in self.pars]
        # ДЕРЖАТЕЛИ НЕТЕКСТОВОГО СОДЕРЖИМОГО: абзацы с рисунком, текстовой
        # рамкой (в PDF-конверсиях один «пустой» абзац держит до 1500 рамок =
        # страницы тома) или полем. В окно замены такие абзацы НЕ попадают —
        # иначе очистка окна уносила рисунок/страницы (ревью 05.09)
        self.holders: set[int] = set()
        for i, p in enumerate(self.pars):
            el = p._p
            for child in el.iter(_qn("w:drawing"), _qn("w:pict"), _qn("w:txbxContent"),
                                 _qn("w:fldChar"), _qn("w:object")):
                # рамка/рисунок ВНУТРИ вложенной рамки принадлежат её абзацу
                anc = child.getparent()
                nested = False
                while anc is not None and anc is not el:
                    if anc.tag == _qn("w:txbxContent"):
                        nested = True
                        break
                    anc = anc.getparent()
                if not nested:
                    self.holders.add(i)
                    break
        self.norm = [_norm(t) for t in self.text]
        self.words = [_sig_words(t) for t in self.text]
        self.garbled = garble_ratio("".join(self.text[:3000]))
        # КОЛОНТИТУЛЫ: в PDF-конверсии верхний/нижний колонтитул повторяется на
        # каждой странице отдельной строкой («ПРИЛОЖЕНИЕ РАСЧЕТ РАССЕИВАНИЯ…»,
        # шифр тома). Пять правок легли в такую строку (05.09) — исключаем
        # строки, повторяющиеся ≥5 раз, из поиска мест и заголовков
        cnt: dict[str, int] = {}
        for nrm in self.norm:
            if nrm:
                cnt[nrm] = cnt.get(nrm, 0) + 1
        # только ДЛИННЫЕ повторы (≥20 символов без пробелов): короткие ячейки
        # «шт», «м3», числа повторяются сотни раз и не колонтитулы (порог без
        # длины вычёркивал 25 % строк тома — всё уходило «вручную»)
        self.noise: set[int] = {i for i, nrm in enumerate(self.norm)
                                if nrm and 20 <= len(nrm) < 160 and cnt[nrm] >= 5}
        # ОГЛАВЛЕНИЕ (тестировщик №4: 5 вставок легли в оглавление — двухстрочный
        # пункт оглавления несёт отточие только во второй строке, и первая
        # проходила как «заголовок»). Зона оглавления = самый плотный кластер
        # строк с отточием в первых 40 % тома; вся зона исключается из поиска
        self.toc: set[int] = set()
        front = max(200, int(len(self.text) * 0.4))
        dotted = [i for i, t in enumerate(self.text[:front]) if re.search(r"\.{5,}|…{3,}", t)]
        groups: list[list[int]] = []
        for i in dotted:
            if groups and i - groups[-1][-1] <= 8:
                groups[-1].append(i)
            else:
                groups.append([i])
        best_g = max(groups, key=len) if groups else []
        if len(best_g) >= 5:
            self.toc = set(range(max(0, best_g[0] - 2), best_g[-1] + 1))
            self.noise |= self.toc
        # слово → множество индексов абзацев (для предфильтра кандидатов)
        self.inv: dict[str, set[int]] = {}
        for i, ws in enumerate(self.words):
            if i in self.noise:
                continue
            for w in ws:
                self.inv.setdefault(w, set()).add(i)
        # ЗАГОЛОВКИ РАЗДЕЛОВ ТОМА (для поиска места ПО ТЕМЕ): в PDF-конверсиях
        # заголовки идут ЗАГЛАВНЫМИ и БЕЗ номеров («ОХРАНА НЕДР»), номера
        # пунктов из ответов («п. 3.5.1») в тексте не существуют (05.09).
        # Кандидат: короткая строка, не колонтитул, не конец фразы, после неё
        # идёт текст; ЗАГЛАВНЫМИ — всегда, обычным регистром — если перед ней
        # пустая/короткая строка (подраздел).
        import re as _re
        # heads: (idx_первой_строки, слова, заголовок ЗАГЛАВНЫМИ?, текст)
        self.heads: list[tuple[int, set[str], bool, str]] = []
        n = len(self.text)
        i = 0
        while i < n:
            dt = decode_garbled(self.text[i]).strip()
            L = len(dt)
            ok = (12 <= L <= 120 and i not in self.noise and "\t" not in dt
                  and dt[-1] not in ".,;:" and _re.search(r"[А-ЯЁа-яё]{4,}", dt))
            if not ok:
                i += 1
                continue
            letters = _re.sub(r"[^А-ЯЁа-яёA-Za-z]", "", dt)
            caps = bool(letters) and letters == letters.upper()
            prev_short = i == 0 or len(self.text[i - 1].strip()) < 40
            if not (caps or (dt[0].isupper() and prev_short)):
                i += 1
                continue
            # ЗАГЛАВНЫЙ заголовок часто разорван PDF на 2–3 строки
            # («ВЫБРОСЫ ЗАГРЯЗНЯЮЩИХ» / «ВЕЩЕСТВ В АТМОСФЕРУ») — склеиваем
            j = i + 1
            title = dt
            if caps:
                while j < n:
                    nx = decode_garbled(self.text[j]).strip()
                    nl = _re.sub(r"[^А-ЯЁа-яёA-Za-z]", "", nx)
                    if 3 <= len(nx) <= 120 and nl and nl == nl.upper() and "\t" not in nx \
                            and _re.search(r"[А-ЯЁ]{3,}", nx) and j not in self.noise:
                        title += " " + nx
                        j += 1
                    else:
                        break
            body = sum(1 for q in range(j, min(n, j + 6)) if len(self.text[q]) > 60)
            if body >= 2:
                ws = _sig_words(title)
                if ws:
                    # last = ПОСЛЕДНЯЯ строка склеенного заголовка: вставка идёт
                    # после неё, а не между строками заголовка (ревью)
                    self.heads.append((i, ws, caps, title[:100], j - 1))
            i = j
        self._head_pos = [h[0] for h in self.heads]
        self.head_lines: set[int] = set()
        for h in self.heads:
            self.head_lines.update(range(h[0], h[4] + 1))

    def heading_end(self, i: int) -> int:
        """Последняя строка заголовка, начатого строкой i: PDF рвёт длинный
        заголовок на 2–3 строки, вставка «после заголовка» в первую из них
        разрывала его (тестировщик №4: 5 разорванных заголовков)."""
        n = len(self.text)
        j = i
        for _ in range(3):
            cur = decode_garbled(self.text[j]).strip()
            if j + 1 >= n or not cur or cur[-1] in ".:;!?":
                break
            nx = decode_garbled(self.text[j + 1]).strip()
            if not nx or len(nx) > 120 or (j + 1) in self.noise or (j + 1) in self.holders:
                break
            nl = re.sub(r"[^А-ЯЁа-яёA-Za-z]", "", nx)
            cl = re.sub(r"[^А-ЯЁа-яёA-Za-z]", "", cur)
            caps_cont = bool(nl) and nl == nl.upper() and bool(cl) and cl == cl.upper() \
                and not re.match(r"^\d", nx)
            if not (caps_cont or nx[0].islower()):
                break
            j += 1
        return j

    def sentence_end(self, i: int) -> int:
        """Строка, где кончается предложение, начатое в строке i (в PDF-конверсии
        абзац = строка; вставка после середины предложения рвала его)."""
        n = len(self.text)
        j = i
        for _ in range(8):
            cur = decode_garbled(self.text[j]).strip()
            if not cur or cur[-1] in ".!?;:" or j + 1 >= n:
                break
            if (j + 1) in self.holders or (j + 1) in self.noise or (j + 1) in self.head_lines \
                    or _HEAD_LINE_RX.match(decode_garbled(self.text[j + 1])):
                break
            j += 1
        return j

    def find_topic_heading(self, topic: set[str], *, strict: bool = False
                           ) -> tuple[int, int, float, str]:
        """Заголовок раздела, лучше всего совпадающий с темой ответа (значимые
        слова «где править» + замечания). Возвращает (idx, end_idx, score,
        текст заголовка); idx=-1 если совпадение слабое. Заголовки ЗАГЛАВНЫМИ
        (главы) надёжнее строк обычного регистра — для последних порог выше.
        strict=True (название в кавычках): достаточно и одного точного слова,
        если оно покрывает ≥ половины заголовка."""
        best = (-1, -1, 0.0, "")
        for pos, (i, ws, caps, title, last) in enumerate(self.heads):
            ov = len(topic & ws)
            if ov == 0:
                continue
            cover = ov / len(ws)
            if strict:
                if ov < 2 and cover < 0.5:
                    continue
            elif ov < 2 or (not caps and (ov < 3 and cover < 0.6)):
                continue
            score = cover + 0.05 * ov + (0.1 if caps else 0.0)
            # слабое совпадение по теме (0.27–0.29 на реальных томах) уводило
            # правку в чужой раздел — лучше честно «вручную»
            if score < 0.35 and not strict:
                continue
            if score > best[2]:
                end = self._head_pos[pos + 1] if pos + 1 < len(self._head_pos) else min(len(self.text), i + 600)
                # возвращаем ПОСЛЕДНЮЮ строку заголовка: вставка — после неё
                best = (last, end, score, title[:80])
        return best

    def window_text(self, i: int, k: int) -> str:
        return "".join(self.norm[i:i + k])

    def find(self, was: str, used: set[int], lo: int = 0, hi: int | None = None):
        """Лучшее окно строк [i, i+k) для «было»: (i, k, score)."""
        hi = len(self.pars) if hi is None else hi
        ww = _sig_words(was)
        wn = _norm(was)
        wg = _grams(wn)
        if len(ww) < 2 or len(wg) < 8:
            return -1, 0, 0.0
        # кандидаты: абзацы, где встречаются ≥2 значимых слова «было»
        cnt: dict[int, int] = {}
        for w in ww:
            for i in self.inv.get(w, ()):
                if lo <= i < hi:
                    cnt[i] = cnt.get(i, 0) + 1
        # топ-60 кандидатов по числу совпавших значимых слов: на 64k строк ×
        # 75 ответов полный перебор окон занимал бы минуты
        cands = sorted(cnt, key=lambda i: -cnt[i])[:60]
        best = (-1, 0, 0.0)
        best_dist = 10 ** 9
        target_len = len(wn)
        for c in cands:
            for start in range(max(lo, c - 3), c + 1):
                if start in used or start in self.noise or start in self.holders:
                    continue
                acc = ""
                for k in range(1, 12):
                    # окно НЕ пересекает уже занятые правкой строки (ревью:
                    # вторая замена затирала первую) и держателей рамок/рисунков
                    j = start + k - 1
                    if start + k > hi or j in self.noise or j in used or j in self.holders:
                        break
                    acc += self.norm[start + k - 1]
                    if len(acc) < target_len * 0.5:
                        continue
                    if len(acc) > target_len * 2.2 + 40:
                        break
                    g = _grams(acc)
                    if not g:
                        continue
                    score = len(wg & g) / len(wg)
                    # штраф за окно сильно длиннее «было» (захват чужого текста)
                    if len(acc) > target_len * 1.6:
                        score *= 0.9
                    # при РАВНОМ сходстве берём окно, ближайшее по длине к «было»:
                    # иначе выигрывало окно с лишней строкой сверху (заголовок
                    # «Раздел 10…» затирался заменой — найдено тестом)
                    dist = abs(len(acc) - target_len)
                    if score > best[2] or (score == best[2] and dist < best_dist):
                        best = (start, k, score)
                        best_dist = dist
        return best

    _FRONT_RX = None   # строки состава проекта / оглавления — не заголовки тела

    def _is_front_matter(self, i: int) -> bool:
        """«3.3.2 717/14/15-П-1/ТКР.ЭС» (состав проекта), «3.3.2 …….. 45»
        (оглавление) — такие строки первыми содержат номер пункта и раньше
        перехватывали вставку «по заголовку» (реальный случай 05.09)."""
        import re as _re
        if _Index._FRONT_RX is None:
            _Index._FRONT_RX = _re.compile(
                r"\d+/\d+/\d+-|\.{4,}\s*\d*\s*$|\s\d{1,3}\s*$|^содержание|^оглавление")
        return bool(_Index._FRONT_RX.search(decode_garbled(self.text[i]).strip().lower()))

    def find_heading(self, hint: str, kind: str = "item"):
        """Индекс ЗАГОЛОВКА пункта/таблицы «hint» В ТЕЛЕ тома: короткая строка,
        НАЧИНАЮЩАЯСЯ с номера (не «рис. 3.5.1» посреди текста), не из состава
        проекта/оглавления, и после неё идёт обычный текст (≥2 из следующих 6
        строк длиннее 60 символов). Первое такое вхождение."""
        import re as _re
        # после номера — пробел и слово с ЗАГЛАВНОЙ («3.3.2 Характеристика…»);
        # строки таблиц «1 п п Наименование», списки «5 - Особо большой», текст
        # «7 время в течении…» — не заголовки (реальные промахи 05.09)
        # заголовок бывает и ЗАГЛАВНЫМИ («3.5.1 ИНТЕНСИВНОСТЬ ДВИЖЕНИЯ»)
        if kind == "table":
            # только ПОДПИСЬ таблицы («Таблица 5.11 – …» в начале строки)
            rx_t = _re.compile(r"^(?i:табл(?:ица|\.))\s*№?\s*" + _re.escape(hint) + r"(?!\d|\.\d)")
            for i, t in enumerate(self.text):
                dt = decode_garbled(t).strip()
                if dt and len(dt) <= 200 and rx_t.match(dt) and i not in self.noise \
                        and not self._is_front_matter(i):
                    return i
            return -1
        rx = _re.compile(r"^(?i:п\.?|пункт|раздел)?\s*№?\s*"
                         + _re.escape(hint) + r"(?![\d])[.)]?\s+[А-ЯЁA-Z][А-ЯЁа-яёA-Za-z]{2,}")
        n = len(self.text)
        for i, t in enumerate(self.text):
            dt = decode_garbled(t).strip()
            if not dt or len(dt) > 140 or "\t" in dt or not rx.match(dt):
                continue
            if self._is_front_matter(i) or i in self.noise:
                continue
            body = sum(1 for j in range(i + 1, min(n, i + 7)) if len(self.text[j]) > 60)
            if body >= 2:
                return i
        return -1


# СТРОГАЯ РАСКЛАДКА (08.09.2026): правка встаёт в том ТОЛЬКО по подтверждённому
# месту — найденное «было», цитата замечания или явный заголовок пункта/таблицы
# из «где править». Тематическое угадывание раздела выключено.
STRICT_PLACEMENT = True


def plan_corrections(doc, answers: list[dict]) -> tuple[list[dict], "_Index"]:
    """ПЛАН правок для одного тома: где и что менять. Общий для preview и записи.
    Элемент: {number, mode: replace|insert|manual|skip, idx, k, score,
    par_text, shall, location, is_table}."""
    ix = _Index(doc)
    used: set[int] = set()
    plan: list[dict] = []
    for a in answers:
        num = a.get("number", "?")
        # ДЕКОДИРУЕМ поля ответа: ИИ мог скопировать «ɢɧɬɟɧɫɢɜɧɨɫɬɶ» из индекса,
        # собранного до фикса кодировки, — иначе мусор уехал бы в том (05.08)
        shall = decode_garbled((a.get("edit_shall") or a.get("correction") or "").strip())
        was = decode_garbled((a.get("edit_was") or "").strip())
        loc = decode_garbled((a.get("edit_location") or "").strip())
        remark = decode_garbled((a.get("remark") or "").strip())
        e = {"number": num, "mode": "manual", "idx": -1, "k": 0, "score": 0.0,
             "par_text": "", "shall": shall, "was": was, "location": loc,
             "sources": [f"{s.get('file', '')} {s.get('loc', '')}".strip()
                         for s in (a.get("sources") or [])[:4]],
             "is_table": _is_md_table(shall), "via": "",
             # документы, которых не хватает, — под них резервируется место
             # в конце тома (ТЗ 08.09: «оставить пустое место и выделить»)
             "attachments": [str(x) for x in (a.get("attachments") or []) if str(x).strip()]}
        if not shall or a.get("no_change") or _same_text(was, shall):
            # нет текста правки / «стало» = «было»
            e["mode"] = "skip"
            e["hint"] = "нет текста правки («стало»)" if not shall else "«стало» совпадает с «было» — менять нечего"
            plan.append(e)
            continue
        meta = _META_RX.search(shall)
        if meta:
            # вместо текста правки — указание, что сделать («см. таблицу выше»,
            # «<значение по методике>», «(существующие строки…)»): в том не вносим
            e["hint"] = f"в «стало» не текст правки, а указание/заглушка: «{meta.group(0)[:50]}»"
            plan.append(e)
            continue
        if a.get("shall_unverified"):
            # «стало» с числами без источника при «Требуется…» — не вносить
            nums = "; ".join(str(x) for x in (a.get("unsupported_numbers") or [])[:8])
            e["hint"] = "«стало» содержит неподтверждённые числа" + (f": {nums}" if nums else "") + " (нужны данные)"
            plan.append(e)
            continue
        # заголовки пунктов/таблиц из «где править» — В ТЕЛЕ тома (не в составе
        # проекта и не в оглавлении); окно поиска — сам пункт (до 400 строк)
        hints = _loc_hints(loc)
        heads = [(h, ix.find_heading(h, kind)) for kind, h in hints]
        heads = [(h, hi_) for h, hi_ in heads if hi_ >= 0]
        if a.get("location_mismatch"):
            # место из ответа не сходится с замечанием (чужой том/раздел) —
            # по заголовку НЕ ставим; остаётся только дословное «было»
            heads = []
            e["hint"] = "место в ответе не соответствует замечанию — проверить том и пункт"
        i, k, s, via = -1, 0, 0.0, ""
        near = None
        if was:
            # 1) «было» внутри нужного пункта — самое надёжное место
            for h, hi_ in heads:
                i2, k2, s2 = ix.find(was, used, hi_, min(len(ix.pars), hi_ + 400))
                if s2 >= 0.45 and s2 > s:
                    i, k, s, via = i2, k2, s2, f"«было» в п. {h}"
            # 2) по всему тому — только при УВЕРЕННОМ сходстве (замена); при
            #    среднем 0.45–0.55 — вставка после найденного места. Ниже 0.45
            #    в чужой текст не лезем (05.09: 0.35 давало «куда попало»)
            if i < 0:
                i2, k2, s2 = ix.find(was, used)
                if s2 >= 0.55:
                    i, k, s, via = i2, k2, s2, "«было» найдено в томе"
                elif s2 >= 0.50:
                    near = (i2, k2, s2)
        if i >= 0:
            i, k, kept = _trim_window(ix, i, k, was, used)
            used.update(range(i, i + k))
            used.update(kept)
            e.update(mode="replace", idx=i, k=k, score=round(s, 2), via=via,
                     kept_heading=decode_garbled(" ".join(ix.text[j] for j in kept)),
                     par_text=decode_garbled(" ".join(ix.text[i:i + k]))[:200])
        elif near:
            # похожее место есть, но не дословно: вставка «рядом» оставляла старый
            # абзац (противоречия 66/82 чел. — тестировщик №3) → вручную с подсказкой
            i2, k2, s2 = near
            e["hint"] = (f"похожий фрагмент (сходство {int(s2 * 100)} %): "
                         f"«{decode_garbled(' '.join(ix.text[i2:i2 + k2]))[:120]}»")
        elif was:
            # «было» есть, но в томе не найдено — под заголовок НЕ вставляем
            # (старый текст остался бы рядом с новым): в ручное размещение
            pass
        else:
            # 3) сразу после заголовка нужного пункта/таблицы (в теле тома) —
            #    только для ДОБАВЛЯЕМОГО текста (без «было»)
            for h, hi_ in heads:
                end_ = ix.heading_end(hi_)
                if hi_ not in used and end_ not in used:
                    e.update(mode="insert", idx=end_, k=1, via=f"после заголовка п. {h}",
                             par_text=decode_garbled(" ".join(ix.text[hi_:end_ + 1]))[:160])
                    used.update(range(hi_, end_ + 1))
                    break
            # 4) ПО ТЕМЕ: раздел тома, чей заголовок совпадает со значимыми
            #    словами «где править» + замечания (в PDF-конверсиях номеров
            #    пунктов нет — только названия). Внутри раздела ещё раз ищем
            #    «было»/цитату замечания (порог ниже: область уже верная);
            #    иначе — сразу после заголовка раздела.
            if e["mode"] == "manual":
                import re as _re2
                # название раздела/таблицы в кавычках из «где править» —
                # самый точный ориентир, пробуем его первым
                quoted = " ".join(_re2.findall(r"[«\"„]([^»\"“]{4,80})[»\"“]", loc))
                ti = -1
                explicit = False      # раздел НАЗВАН в «где править» (в кавычках)
                if quoted:
                    ti, tend, ts, ttitle = ix.find_topic_heading(_sig_words(quoted), strict=True)
                    explicit = ti >= 0
                if ti < 0:
                    topic = _sig_words(loc) | _sig_words(remark)
                    ti, tend, ts, ttitle = ix.find_topic_heading(topic)
                if ti >= 0:
                    placed = False
                    if was:
                        i4, k4, s4 = ix.find(was, used, ti, tend)
                        if s4 >= 0.40:
                            used.update(range(i4, i4 + k4))
                            e.update(mode="replace" if s4 >= 0.55 else "insert",
                                     idx=i4 if s4 >= 0.55 else i4 + k4 - 1,
                                     k=k4 if s4 >= 0.55 else 1, score=round(s4, 2),
                                     via=f"«было» в разделе «{ttitle[:40]}»",
                                     par_text=decode_garbled(" ".join(ix.text[i4:i4 + k4]))[:160])
                            placed = True
                    if not placed and remark:
                        i5, k5, s5 = ix.find(remark, used, ti, tend)
                        if s5 >= 0.40:
                            used.update(range(i5, i5 + k5))
                            e.update(mode="insert", idx=ix.sentence_end(i5 + k5 - 1), k=1, score=round(s5, 2),
                                     via=f"цитата замечания в разделе «{ttitle[:40]}»",
                                     par_text=decode_garbled(" ".join(ix.text[i5:i5 + k5]))[:160])
                            placed = True
                    if not placed and ti not in used and (explicit or not STRICT_PLACEMENT):
                        # после заголовка раздела: если раздел НАЗВАН в «где
                        # править» — это явный якорь; угадывание «по теме» без
                        # подтверждения цитатой ОТКЛЮЧЕНО (08.09.2026: «опять
                        # куда попало») — такие правки идут в раздел ручного
                        # размещения с указанием раздела-кандидата
                        used.add(ti)
                        e.update(mode="insert", idx=ti, k=1, score=round(ts, 2),
                                 via=(f"в раздел «{ttitle[:40]}» (назван в «где править»)"
                                      if explicit else f"в раздел «{ttitle[:40]}» (по теме)"),
                                 par_text=ttitle)
                    elif not placed:
                        e["hint"] = f"вероятный раздел: «{ttitle[:60]}»"
            # 5) по ТЕКСТУ ЗАМЕЧАНИЯ по всему тому — только при высоком сходстве
            if e["mode"] == "manual" and remark:
                i3, k3, s3 = ix.find(remark, used)
                if s3 >= 0.50:
                    last = ix.sentence_end(i3 + k3 - 1)
                    used.update(range(i3, last + 1))
                    e.update(mode="insert", idx=last, k=1, score=round(s3, 2),
                             via="по цитате из замечания",
                             par_text=decode_garbled(" ".join(ix.text[i3:i3 + k3]))[:160])
        plan.append(e)
    return plan, ix


_HEAD_LINE_RX = re.compile(r"^\s*\d{1,2}(?:\.\d{1,2}){1,4}\.?\s+[А-ЯЁA-Z]")
_META_RX = re.compile(r"см\.\s*поле|edit_shall|edit_was|<[^<>\n]{2,80}>|\{[^{}\n]{2,60}\}|"
                      r"\(существующ[^)]{0,80}\)|см\.\s*(?:таблиц\w*\s*)?(?:выше|ниже)|"
                      r"^\s*в\s+раздел\w*\s+[^.]{0,60}\bдобавить", re.I | re.M)


def _same_text(a: str, b: str) -> bool:
    """«стало» ≈ «было» (без переносов, тире, ё, регистра, пробелов)."""
    import difflib
    na, nb = _norm(a or ""), _norm(b or "")
    if not na or not nb:
        return False
    if na == nb:
        return True
    if abs(len(na) - len(nb)) > 0.15 * max(len(na), len(nb)):
        return False
    return difflib.SequenceMatcher(None, na, nb, autojunk=False).ratio() >= 0.97


def _trim_window(ix: "_Index", i: int, k: int, was: str, used: set[int]) -> tuple[int, int, list[int]]:
    """Окно замены по ФАКТИЧЕСКИ совпавшим строкам (тестировщик №4): строки с
    краёв, которых в «было» нет, не стираются; заголовок пункта, попавший в
    «было», остаётся строкой (kept); окно, оборванное на переносе «коэффици-»,
    дотягивается до следующей строки."""
    wg = _grams(_norm(was))

    def cover(j: int) -> float:
        g = _grams(ix.norm[j])
        return (len(g & wg) / len(g)) if g else 1.0
    while k > 1 and cover(i + k - 1) < 0.3:
        k -= 1
    while k > 1 and cover(i) < 0.3:
        i += 1
        k -= 1
    kept: list[int] = []
    if k > 1 and (_HEAD_LINE_RX.match(decode_garbled(ix.text[i])) or i in ix.head_lines):
        end_ = min(ix.heading_end(i), i + k - 2)
        kept = list(range(i, end_ + 1))
        k -= len(kept)
        i = end_ + 1
    last = i + k - 1
    tail = decode_garbled(ix.text[last]).rstrip()
    nxt = last + 1
    if tail[-1:] in "-‐‑" and nxt < len(ix.text) and nxt not in used and nxt not in ix.noise \
            and nxt not in ix.holders and nxt not in ix.head_lines:
        k += 1
    return i, k, kept


def _strip_heading(shall: str, heading: str) -> str:
    """Заголовок пункта сохранён строкой — не повторяем его в начале «стало»."""
    m = re.match(r"^\s*(\d+(?:\.\d+)+)\.?\s*", heading or "")
    if not m:
        return shall
    m2 = re.match(r"^\s*" + re.escape(m.group(1)) + r"\.?\s*", shall)
    if not m2:
        return shall
    rest = shall[m2.end():]
    title = heading[m.end():].strip()
    nt = _norm(title)
    if nt and _norm(rest).startswith(nt[: max(10, int(len(nt) * 0.8))]):
        rest = rest[len(title):].lstrip(" .:–—-\n")
    return rest.strip() or shall


def _span_context(lines: list[str], was: str) -> tuple[str, str]:
    """Что в окне строк стоит ДО и ПОСЛЕ цитаты «было» (сохраняется при замене).
    Совпадение ищется difflib'ом по тексту без изменения длины (регистр, ё→е),
    чтобы индексы совпадали с оригиналом; при слабом покрытии (< 60 %) —
    контекст не выделяется (как раньше: заменяется всё окно)."""
    import difflib
    if not lines or not was:
        return "", ""
    wt = "\n".join(decode_garbled(l) for l in lines)
    a = wt.lower().replace("ё", "е")
    b = decode_garbled(was).lower().replace("ё", "е").strip()
    if len(b) < 8:
        return "", ""
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    blocks = [bl for bl in sm.get_matching_blocks() if bl.size >= 4]
    if not blocks:
        return "", ""
    cov = sum(bl.size for bl in blocks) / max(1, len(b))
    if cov < 0.6:
        return "", ""
    s0 = blocks[0].a
    s1 = blocks[-1].a + blocks[-1].size
    # НЕ РВЁМ СЛОВО: границы совпадения — по границам слов; перенос «коэффици-» +
    # «ент» на следующей строке — одно слово (тестировщик №4: обрывки «ент 1,19.»)
    while 0 < s0 < len(wt) and wt[s0 - 1].isalnum() and wt[s0].isalnum():
        s0 -= 1
    if 0 < s1 < len(wt) - 1 and wt[s1 - 1] in "-‐‑" and wt[s1] == "\n" and wt[s1 + 1].isalpha():
        s1 += 1
    while 0 < s1 < len(wt) and (wt[s1 - 1].isalnum() or wt[s1 - 1] == "\n") and wt[s1].isalnum():
        s1 += 1
    # короткий хвост ТОГО ЖЕ предложения (до 25 знаков) уходит вместе с «было»
    if s1 > 0 and wt[s1 - 1] not in ".!?":
        mt = re.match(r"[^.!?]{0,25}[.!?]", wt[s1:])
        if mt:
            s1 += mt.end()
    first_len = len(lines[0])
    last_start = len(wt) - len(lines[-1])
    pre = wt[:s0].strip() if s0 <= first_len else ""
    post = wt[s1:].strip() if s1 >= last_start else ""
    # знак препинания на стыке принадлежит заменяемой фразе — не дублируем
    post = post.lstrip(".,;:) ").strip()
    pre = pre.rstrip("(").strip()
    # обрывки короче 3 знаков (знаки препинания) — не тащим; номер в списке
    # («1.», «2)», «–») — сохраняем
    pre = pre if len(pre) >= 3 or re.fullmatch(r"\d{1,2}[.)]|[-–•]", pre) else ""
    post = post if len(post) >= 3 else ""
    return pre, post


def _blank_par_text(par) -> int:
    """Стереть ТЕКСТ абзаца (все w:t, в т.ч. внутри w:hyperlink / w:ins /
    w:smartTag / строчных sdt), НЕ трогая рисунки, разрывы, поля и текст
    ВЛОЖЕННЫХ текстовых рамок (w:txbxContent — это другие абзацы тома).
    Возвращает число очищенных w:t."""
    from docx.oxml.ns import qn
    el = par._p
    n = 0
    for t in el.iter(qn("w:t")):
        anc = t.getparent()
        nested = False
        while anc is not None and anc is not el:
            if anc.tag == qn("w:txbxContent"):
                nested = True
                break
            anc = anc.getparent()
        if nested:
            continue
        if t.text:
            t.text = ""
            n += 1
    return n


def _std_run(run):
    """Стандартный шрифт для ВСТАВЛЯЕМОГО текста: в томах-конверсиях шрифт
    кастомный (глифы по смещённым кодам) — обычная кириллица в нём показалась
    бы кракозябрами."""
    from docx.oxml.ns import qn
    from docx.shared import Pt
    run.font.name = "Times New Roman"
    rpr = run._r.get_or_add_rPr()
    rf = rpr.find(qn("w:rFonts"))
    if rf is None:
        rf = rpr.makeelement(qn("w:rFonts"), {})
        rpr.insert(0, rf)
    for k in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        rf.set(qn(k), "Times New Roman")
    run.font.size = Pt(12)


def _apply_plan(doc, plan: list[dict], ix: "_Index") -> dict:
    from docx.enum.text import WD_COLOR_INDEX
    from docx.shared import Pt
    stats = {"replace": 0, "insert": 0, "manual": 0, "skip": 0}
    manual: list[dict] = []

    def _yellow(run):
        run.font.highlight_color = WD_COLOR_INDEX.YELLOW

    def _mark(par, num):
        r = par.add_run(f" [изм. по замечанию №{num}]")
        _std_run(r)
        r.italic = True
        r.font.size = Pt(8)
        _yellow(r)

    def _set_par(par, text):
        """Заменить ТЕКСТ абзаца на «стало», сохранив нетекстовое содержимое:
        рисунки, разрывы, поля, вложенные рамки остаются (ревью: Run.text=''
        удалял w:drawing/w:pict/w:fldChar); стирается и текст внутри
        гиперссылок/w:ins/w:smartTag (ревью: оставался хвост «старое+новое»)."""
        _blank_par_text(par)
        base = par.add_run(text)
        # новый текст — В НАЧАЛО абзаца (сразу после свойств), а не в хвост
        # за рисунками
        r_el = base._r
        par._p.remove(r_el)
        ppr = par._p.pPr
        if ppr is not None:
            ppr.addnext(r_el)
        else:
            par._p.insert(0, r_el)
        _std_run(base)
        _yellow(base)
        return base

    def _table_after(par, rows):
        if not rows:
            return
        ncols = max(len(r) for r in rows)
        tbl = doc.add_table(rows=len(rows), cols=ncols)
        try:
            tbl.style = "Table Grid"
        except KeyError:
            # в томах-конверсиях из PDF стандартных стилей нет — рисуем
            # границы вручную (w:tblBorders), иначе таблица без линий
            from docx.oxml.ns import qn as _qn
            from docx.oxml import OxmlElement
            tpr = tbl._tbl.tblPr
            borders = OxmlElement("w:tblBorders")
            for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
                b = OxmlElement(f"w:{side}")
                b.set(_qn("w:val"), "single")
                b.set(_qn("w:sz"), "4")
                b.set(_qn("w:color"), "000000")
                borders.append(b)
            tpr.append(borders)
        for i, row in enumerate(rows):
            for j in range(ncols):
                cell = tbl.cell(i, j)
                cell.text = row[j] if j < len(row) else ""
                for p in cell.paragraphs:
                    for r in p.runs:
                        _std_run(r)
                        r.font.size = Pt(9)
                        r.bold = (i == 0)
                        _yellow(r)
        par._p.addnext(tbl._tbl)
        # ОБЯЗАТЕЛЬНО абзац ПОСЛЕ таблицы: если таблица оказывается последним
        # элементом ячейки (w:tc), Word считает файл повреждённым и не
        # открывает его (реальный случай: том 6.1, «Таблица 10.2» в ячейке —
        # «откорректированный файл не открыть, пишет ошибка»); в теле документа
        # абзац ещё и не даёт двум таблицам подряд слипнуться в одну
        from docx.oxml.ns import qn as _qn2
        tbl._tbl.addnext(par._p.makeelement(_qn2("w:p"), {}))

    for e in plan:
        mode = e["mode"]
        if mode == "skip":
            stats["skip"] += 1
            continue
        if mode == "manual":
            manual.append(e)
            stats["manual"] += 1
            continue
        par = ix.pars[e["idx"]]
        shall, num = e["shall"], e["number"]
        head_lines = [l for l in shall.splitlines() if l.strip() and l.count("|") < 2]
        body = " ".join(head_lines) if e["is_table"] else shall
        if mode == "replace":
            # ТОЧНАЯ ЗАМЕНА (тестировщик №3, 16.09: замена стирала соседний текст —
            # заголовки, шапки таблиц, полустроки): меняем только сам фрагмент
            # «было», текст до и после него в окне сохраняется
            lines = [ix.text[j] for j in range(e["idx"], e["idx"] + e["k"])]
            pre, post = _span_context(lines, e.get("was") or "")
            if e.get("kept_heading"):
                body = _strip_heading(body, e["kept_heading"])
            body_new = (pre + " " if pre else "") + body + (" " + post if post else "")
            _set_par(par, body_new)
            _mark(par, num)
            # остальные строки окна — стираем ТОЛЬКО текст (рисунки/рамки/поля
            # остаются; текст перенесён в первую строку)
            for j in range(e["idx"] + 1, e["idx"] + e["k"]):
                _blank_par_text(ix.pars[j])
        else:  # insert после заголовка
            np = _insert_paragraph_after(par, [])
            _set_par(np, body)
            _mark(np, num)
            par = np
        if e["is_table"]:
            from docx.oxml.ns import qn as _qn
            in_box = any(anc.tag == _qn("w:txbxContent") for anc in par._p.iterancestors())
            if in_box:
                # внутри текстовой рамки настоящую таблицу Word может не открыть —
                # кладём строки текстом «ячейка | ячейка»
                for row in _md_table_rows(shall):
                    np2 = _insert_paragraph_after(par, [])
                    _set_par(np2, " | ".join(row))
                    par = np2
            else:
                _table_after(par, _md_table_rows(shall))
        stats[mode] += 1

    if manual:
        from docx.enum.text import WD_BREAK
        doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        h = doc.add_paragraph()
        hr = h.add_run("ПРАВКИ ПО ЗАМЕЧАНИЯМ, ТРЕБУЮЩИЕ РУЧНОГО РАЗМЕЩЕНИЯ "
                       "(подтверждённое место в томе не найдено — перенести в "
                       "указанный пункт и удалить этот раздел)")
        _std_run(hr)
        hr.bold = True
        for e in manual:
            p = doc.add_paragraph()
            r1 = p.add_run(f"№{e['number']}. ")
            _std_run(r1)
            r1.bold = True
            where = e["location"] or "место в замечании не указано"
            if e.get("pdf_page"):
                where += f"; в PDF тома — стр. {e['pdf_page']}" + (f" ({e['pdf_note']})" if e.get("pdf_note") else "")
            if e.get("hint"):
                where += f" ({e['hint']})"
            r2 = p.add_run(f"Куда: {where}. ")
            _std_run(r2)
            r2.italic = True
            if e.get("was"):
                rw = p.add_run(f"БЫЛО: {e['was'][:600]} ")
                _std_run(rw)
                rw.italic = True
                rs = p.add_run("СТАЛО: ")
                _std_run(rs)
                rs.bold = True
            r3 = p.add_run(e["shall"])
            _std_run(r3)
            _yellow(r3)
    # ЗАРЕЗЕРВИРОВАННЫЕ ПРИЛОЖЕНИЯ: под каждый недостающий документ — отдельный
    # лист с выделенной пустой рамкой-заглушкой (ТЗ 08.09)
    reserved = [(e["number"], att) for e in plan if e["mode"] != "skip"
                for att in (e.get("attachments") or [])]
    stats["reserved"] = len(reserved)
    if reserved:
        # один раздел-перечень с выделенным пустым местом под КАЖДЫЙ документ
        # (15.09: по 70 документов на том — отдельный лист под каждый раздувал
        # том на 70 страниц; юзеру нужно «пустое место, выделенное»)
        from docx.enum.text import WD_BREAK
        doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        h = doc.add_paragraph()
        hr = h.add_run("ПРИЛОЖЕНИЯ (ЗАРЕЗЕРВИРОВАНО) ПОД ДОКУМЕНТЫ ПО ЗАМЕЧАНИЯМ "
                       "(вложить после получения, заглушки удалить)")
        _std_run(hr)
        hr.bold = True
        _yellow(hr)
        seen: dict[str, list[str]] = {}
        order: list[str] = []
        for num, att in reserved:
            key = att.strip().lower()
            if key not in seen:
                seen[key] = []
                order.append(att.strip())
            if str(num) not in seen[key]:
                seen[key].append(str(num))
        stats["reserved"] = len(order)
        for n, att in enumerate(order, start=1):
            p = doc.add_paragraph()
            r = p.add_run(f"Приложение (зарезервировано) {n}. {att} — по замечаниям № "
                          f"{', '.join(seen[att.strip().lower()])}")
            _std_run(r)
            r.bold = True
            pe = doc.add_paragraph()
            re_ = pe.add_run("МЕСТО ДЛЯ ВСТАВКИ: " + "_" * 60)
            _std_run(re_)
            _yellow(re_)
    return stats


_REPORT_NAME = "КОРР_финал-проверка"


def verify_corrected(out_path, plan: list[dict]) -> list[dict]:
    """ФИНАЛ-ПРОВЕРКА тома после записи (ТЗ 08.09): по каждому ответу — встала
    ли правка (метка «[изм. по замечанию №N]» ровно один раз), ушла ли в раздел
    ручного размещения, зарезервированы ли приложения."""
    from docx import Document
    d = Document(str(out_path))
    texts = [p.text for p in _all_paragraphs(d)]
    full = "\n".join(texts)
    rows = []
    for e in plan:
        num = str(e["number"])
        mark = f"[изм. по замечанию №{num}]"
        cnt = full.count(mark)
        if e["mode"] == "skip":
            status, note = "пропуск", e.get("hint") or "у ответа нет текста правки («стало»)"
        elif e["mode"] == "manual":
            ok = f"№{num}. " in full
            status = "вручную" if ok else "✗ НЕ ВНЕСЕНО"
            note = ("в разделе ручного размещения" + (f"; {e['hint']}" if e.get("hint") else "")
                    if ok else "запись не найдена")
        else:
            if cnt == 1:
                status = "✓ заменено" if e["mode"] == "replace" else "✓ вставлено (старый текст не тронут)"
                note = f"{e['mode']}: {e.get('via', '')}"
            elif cnt == 0:
                status, note = "✗ НЕ ВНЕСЕНО", "метка правки в томе не найдена"
            else:
                status, note = "⚠ дубль", f"метка встречается {cnt} раз"
        rows.append({"number": num, "mode": e["mode"], "status": status, "note": note,
                     "where": (e.get("par_text") or e.get("location") or "")[:120],
                     "location": e.get("location", ""),
                     "was": (e.get("was") or "")[:600], "shall": (e.get("shall") or "")[:1200],
                     "sources": list(e.get("sources") or []),
                     "attachments": list(e.get("attachments") or [])})
    return rows


def _write_report(project: str, report: dict) -> Path:
    """Отчёт финал-проверки: JSON (для интерфейса) + docx-таблица (для папки out)."""
    out_dir = project_paths(project)["out"]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{_REPORT_NAME}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    doc = Document()
    from .common import set_default_font, add_title, add_heading
    set_default_font(doc)
    add_title(doc, "Финал-проверка откорректированного раздела")
    doc.add_paragraph(f"Объект: {project}. Сформировано: {report.get('at', '')}. "
                      f"Итог: заменено по месту {report['stats'].get('replace', 0)}, "
                      f"вставлено под заголовок пункта (старый текст не тронут) {report['stats'].get('insert', 0)}, "
                      f"вручную {report['stats'].get('manual', 0)}, "
                      f"пропущено (нет правки / без изменений) {report['stats'].get('skip', 0)}, "
                      f"не внесено {report['stats'].get('missing', 0)}, "
                      f"приложений зарезервировано {report['stats'].get('reserved', 0)}.")
    # ПЕРЕЧЕНЬ ИЗМЕНЕНИЙ (вердикт dex 15.09: главный сдаваемый документ —
    # реестр правок с точной привязкой: № · том · где · БЫЛО · СТАЛО ·
    # основание · способ внесения)
    for vol in report.get("volumes", []):
        add_heading(doc, vol["volume"], level=1)
        rows = vol.get("rows") or []
        pdf = vol.get("pdf") or {}
        if pdf.get("output"):
            doc.add_paragraph(f"PDF с правками поверх оригинала: {Path(pdf['output']).name} — "
                              f"по месту {pdf.get('placed', 0)} (подсветка «было» + выноска со «стало», "
                              f"закладки «★ ПРАВКА №…»), у заголовка пункта {pdf.get('at_heading', 0)} "
                              f"(закладки «☆»), в сводке на первой странице {pdf.get('loose', 0)} — "
                              f"всего {pdf.get('total', 0)} правок.")
        if not rows:
            doc.add_paragraph("Правок для этого тома нет.")
            continue
        pdf_rows = {str(r.get("number")): r for r in (pdf.get("rows") or [])}
        tbl = doc.add_table(rows=1, cols=7)
        try:
            tbl.style = "Table Grid"
        except KeyError:
            pass
        for j, hdr in enumerate(("№", "Где (том, пункт)", "БЫЛО", "СТАЛО", "Основание (источник в ПД)",
                                 "Способ внесения", "Приложения")):
            tbl.rows[0].cells[j].text = hdr
        for r in rows:
            c = tbl.add_row().cells
            pr = pdf_rows.get(str(r["number"]))
            way = r["status"] + (f"; в PDF: {pr['status']}" + (f" стр. {pr['page']}" if pr.get("page") else "")
                                 if pr else "")
            c[0].text = r["number"]
            c[1].text = (r.get("location") or r.get("where") or "")
            c[2].text = r.get("was") or "—"
            c[3].text = r.get("shall") or "—"
            c[4].text = "; ".join(r.get("sources") or []) or "—"
            c[5].text = (way + (f"\n{r['note']}" if r.get("note") else "")).strip()
            c[6].text = "; ".join(r["attachments"])
    p = out_dir / f"{_REPORT_NAME}.docx"
    doc.save(str(p))
    return p


def last_report(project: str) -> dict:
    p = project_paths(project)["out"] / f"{_REPORT_NAME}.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def repair_structure(doc) -> int:
    """Страховка перед сохранением: OOXML требует, чтобы ячейка таблицы (w:tc),
    текстовая рамка (w:txbxContent) и содержимое sdt заканчивались АБЗАЦЕМ, а
    тело документа — абзацем перед w:sectPr. Нарушение = «Word обнаружил
    нечитаемое содержимое». Добавляет пустые w:p где нужно; возвращает число
    ремонтов (в норме 0)."""
    from docx.oxml.ns import qn
    body = doc.element.body
    fixes = 0
    block_tags = {qn("w:p"), qn("w:tbl")}
    for tag in ("w:tc", "w:txbxContent", "w:sdtContent"):
        for el in body.iter(qn(tag)):
            kids = [c for c in el if c.tag not in (qn("w:tcPr"), qn("w:sdtPr"), qn("w:sdtEndPr"))]
            if not kids or kids[-1].tag == qn("w:p"):
                continue
            # sdtContent бывает СТРОЧНЫМ (внутри абзаца: дети w:r/w:hyperlink),
            # ячеечным (w:tc) или строковым (w:tr) — туда абзац класть НЕЛЬЗЯ
            # (ревью: w:p внутри w:p делал файл битым). Чиним только блочные.
            if tag == "w:sdtContent" and not all(k.tag in block_tags for k in kids):
                continue
            el.append(el.makeelement(qn("w:p"), {}))
            fixes += 1
    blocks = [c for c in body if c.tag != qn("w:sectPr")]
    if blocks and blocks[-1].tag != qn("w:p"):
        blocks[-1].addnext(body.makeelement(qn("w:p"), {}))
        fixes += 1
    return fixes


# кэш планов: предпросмотр и запись идут подряд, а разбор тома-конверсии
# (60k строк) стоит ~13 с на том — не делаем его дважды
_PLAN_CACHE: dict[tuple, tuple] = {}
_PLAN_TTL = 600.0


def _answers_key(answers: list[dict]) -> tuple:
    """Ключ кэша плана: любое изменение полей, влияющих на МЕСТО и ТЕКСТ правки
    (ревью: раньше учитывались только длины «стало»/«правки», и после правки
    «было»/«где править» пользователем предпросмотр показывал старый план)."""
    import hashlib
    h = hashlib.sha1()
    for a in answers:
        for f in ("number", "status", "edit_shall", "correction", "edit_was",
                  "edit_location", "remark", "user_answer", "oos_volume"):
            h.update(str(a.get(f) or "").encode("utf-8", "replace"))
            h.update(b"\x1f")
        h.update(b"\x1e")
    return (len(answers), h.hexdigest())


def _plan_for(src, mine: list[dict]):
    """(doc, plan, ix) для тома — из кэша, если том и ответы не менялись."""
    import time as _t
    from docx import Document
    key = (str(src), src.stat().st_mtime, _answers_key(mine))
    hit = _PLAN_CACHE.get(key)
    if hit and _t.time() - hit[3] < _PLAN_TTL:
        return hit[0], hit[1], hit[2]
    doc = Document(str(src))
    plan, ix = plan_corrections(doc, mine)
    # держим не больше 3 томов (по 30 МБ каждый)
    if len(_PLAN_CACHE) >= 3:
        _PLAN_CACHE.pop(next(iter(_PLAN_CACHE)))
    _PLAN_CACHE[key] = (doc, plan, ix, _t.time())
    return doc, plan, ix


def _volume_answers(answers: list[dict], srcs: list, si: int, src) -> list[dict]:
    """Ответы данного тома: по полю «Том ООС» и по «Том X.Y» в тексте ответа.
    Ответ, называющий несколько томов («Том 6.1, Том 6.2, Том 6.3»), идёт в
    КАЖДЫЙ из них; не отнесённые ни к одному тому — в первый."""
    if len(srcs) <= 1:
        mine = list(answers)
    else:
        matched_ids = {id(a) for s2 in srcs for a in answers if _match_volume(a, s2)}
        mine = [a for a in answers if _match_volume(a, src)]
        if si == 0:
            mine += [a for a in answers if id(a) not in matched_ids]
    # ПРАВКА ПО ТОМУ (v0.55): если ИИ дал volume_edits для этого тома (свои
    # числа по пусковому комплексу) — подставляем их вместо общих полей
    tok = _src_volume_token(src)
    out = []
    for a in mine:
        ve = (a.get("volume_edits") or {}).get(tok) if tok else None
        top_shall = (a.get("edit_shall") or "").strip()
        if isinstance(ve, dict) and (ve.get("edit_shall") or "").strip() \
                and _META_RX.search(ve["edit_shall"]) and top_shall and not _META_RX.search(top_shall):
            # ИИ в правке по тому сослался на общее поле («см. поле edit_shall») —
            # берём общий текст правки, место и «было» — из правки по тому
            a2 = dict(a)
            a2["edit_location"] = ve.get("edit_location") or a.get("edit_location", "")
            a2["edit_was"] = ve.get("edit_was") or a.get("edit_was", "")
            out.append(a2)
        elif isinstance(ve, dict) and (ve.get("edit_shall") or "").strip():
            a2 = dict(a)
            a2["edit_location"] = ve.get("edit_location") or a.get("edit_location", "")
            a2["edit_was"] = ve.get("edit_was") or ""
            a2["edit_shall"] = ve["edit_shall"]
            out.append(a2)
        else:
            out.append(a)
    return out


def _placed_text(e: dict) -> str:
    via = f" [{e['via']}]" if e.get("via") else ""
    if e["mode"] == "replace":
        return (f"ЗАМЕНА {e['k']} стр. (сходство {int(e['score'] * 100)}%){via}: "
                f"«{e['par_text'][:120]}…»")
    if e["mode"] == "insert":
        return f"ВСТАВКА после «{e['par_text'][:80]}»{via}"
    if e["mode"] == "skip":
        return "пропуск: у ответа нет текста правки («стало»/«правка»)"
    return ("в конец тома, раздел «требуют ручного внесения»"
            + (f" (указано: {e['location'][:70]})" if e["location"] else ""))


def preview_corrections(project: str, sources: list) -> dict:
    """DRY-RUN: тот же план, что и при записи — что и КУДА встанет, с цитатой
    заменяемого фрагмента (декодированной) и оценкой сходства."""
    from docx import Document
    data = _load_answers(project)
    answers = [a for a in data.get("answers", [])
               if a.get("status") in ("accepted", "edited")]
    srcs = [Path(s) for s in sources if s]
    result = {"volumes": [], "total": 0, "accepted": len(answers),
              "stats": {"replace": 0, "insert": 0, "manual": 0, "skip": 0}}
    for si, src in enumerate(srcs):
        mine = _volume_answers(answers, srcs, si, src)
        vol = {"volume": src.name, "changes": [], "answers": len(mine)}
        try:
            doc, plan, ix = _plan_for(src, mine)
        except Exception as e:  # noqa: BLE001
            vol["error"] = f"том не читается ({e}) — запись правок для него не выполнится"
            result["volumes"].append(vol)
            continue
        if ix.garbled > 0.03:
            vol["warning"] = (
                "том — конверсия из PDF с испорченной кодировкой текста; места "
                "найдены по восстановленному тексту, вставки сделаны стандартным "
                "шрифтом. Для чистого результата лучше исходный Word-том.")
        for e in plan:
            vol["changes"].append({"number": e["number"], "placed": _placed_text(e),
                                   "correction": e["shall"][:300], "mode": e["mode"]})
            result["stats"][e["mode"]] = result["stats"].get(e["mode"], 0) + 1
        result["volumes"].append(vol)
        result["total"] += len(plan)
    return result


def write_corrected_volumes(project: str, sources: list) -> tuple[list[Path], list[str]]:
    """Откорректированные тома: план → применение → *_КОРР.docx."""
    from docx import Document
    data = _load_answers(project)
    answers = [a for a in data.get("answers", [])
               if a.get("status") in ("accepted", "edited")]
    srcs = [Path(s) for s in sources if s]
    out_dir = project_paths(project)["out"]
    out_dir.mkdir(parents=True, exist_ok=True)
    outs: list[Path] = []
    failed: list[str] = []
    report: dict = {"at": datetime.now().isoformat(timespec="seconds"), "volumes": [],
                    "stats": {"replace": 0, "insert": 0, "manual": 0, "skip": 0,
                              "missing": 0, "reserved": 0}}
    for si, src in enumerate(srcs):
        mine = _volume_answers(answers, srcs, si, src)
        try:
            doc, plan, ix = _plan_for(src, mine)
        except PermissionError:
            raise
        except Exception as e:  # noqa: BLE001
            failed.append(f"{src.name}: {e}")
            report["volumes"].append({"volume": src.name, "error": str(e)[:200], "rows": []})
            print(f"[m5] ПРОПУЩЕН {src.name}: {e}", flush=True)
            continue
        # документ сейчас будет МУТИРОВАН — из кэша вон ДО применения (ревью:
        # при сбое сохранения — том открыт в Word — изменённый doc оставался в
        # кэше, и повторная запись вносила все правки второй раз)
        for k in [k for k in _PLAN_CACHE if k[0] == str(src)]:
            _PLAN_CACHE.pop(k, None)
        # ПРАВКИ ПОВЕРХ ОРИГИНАЛЬНОГО PDF — ДО записи docx: PDF-модуль находит место
        # точнее (тестировщик №4), его страницы идут в раздел ручного размещения
        pdf_rep = None
        orig_pdf = src.parent / "_orig" / f"{src.stem}.pdf"
        if orig_pdf.exists():
            try:
                from .pdf_patch import annotate_pdf
                items = [{"number": e["number"], "edit_was": e.get("was", ""),
                          "edit_shall": e["shall"], "edit_location": e.get("location", ""),
                          "location_mismatch": "не соответствует замечанию" in (e.get("hint") or "")}
                         for e in plan if e["mode"] != "skip"]
                pdf_rep = annotate_pdf(orig_pdf, items, out_dir / f"{src.stem}_ПРАВКИ.pdf")
                by_num = {str(r.get("number")): r for r in pdf_rep.get("rows") or []}
                for e in plan:
                    r = by_num.get(str(e["number"]))
                    if r and r.get("page") and r.get("status") != "сводка на стр. 1":
                        e["pdf_page"] = r["page"]
                        e["pdf_note"] = "цитата «было» подсвечена" if r.get("status") == "аннотация" else "выноска у пункта"
            except Exception as ex:  # noqa: BLE001
                pdf_rep = {"error": str(ex)[:200]}
                print(f"[m5] {src.name}: PDF-аннотации не удались: {ex}", flush=True)
        stats = _apply_plan(doc, plan, ix)
        fixes = repair_structure(doc)
        if fixes:
            print(f"[m5] {src.name}: структура починена ({fixes}) — ячейка/рамка "
                  f"заканчивалась таблицей, Word такой файл не открыл бы", flush=True)
        out = out_dir / f"{src.stem}_КОРР.docx"
        doc.save(str(out))
        # документ мутирован — из кэша вон, иначе повторная запись легла бы
        # поверх уже внесённых правок
        for k in [k for k in _PLAN_CACHE if k[0] == str(src)]:
            _PLAN_CACHE.pop(k, None)
        outs.append(out)
        # ФИНАЛ-ПРОВЕРКА записанного тома (ТЗ 08.09)
        try:
            rows = verify_corrected(out, plan)
        except Exception as e:  # noqa: BLE001
            rows = [{"number": "—", "mode": "", "status": "⚠ проверка не выполнена",
                     "note": str(e)[:160], "where": "", "attachments": []}]
        for r in rows:
            if r["status"].startswith("✗"):
                report["stats"]["missing"] += 1
        for k in ("replace", "insert", "manual", "skip", "reserved"):
            report["stats"][k] += int(stats.get(k, 0))
        vol_rep = {"volume": src.name, "output": out.name, "rows": rows, "stats": stats}
        # ПРАВКИ ПОВЕРХ ОРИГИНАЛЬНОГО PDF (v0.55): если том загружали как PDF,
        # оригинал лежит в _orig — кладём подсветки/выноски/закладки в его копию
        if pdf_rep is not None:
            try:
                vol_rep["pdf"] = pdf_rep
                if pdf_rep.get("error"):
                    raise RuntimeError(pdf_rep["error"])
                report["stats"]["pdf_annotated"] = report["stats"].get("pdf_annotated", 0) + vol_rep["pdf"]["placed"]
                report["stats"]["pdf_at_heading"] = report["stats"].get("pdf_at_heading", 0) + vol_rep["pdf"].get("at_heading", 0)
                report["stats"]["pdf_loose"] = report["stats"].get("pdf_loose", 0) + vol_rep["pdf"].get("loose", 0)
                print(f"[m5] {src.name}: PDF — по месту {vol_rep['pdf']['placed']}, у пункта "
                      f"{vol_rep['pdf'].get('at_heading', 0)}, в сводке {vol_rep['pdf'].get('loose', 0)} "
                      f"из {vol_rep['pdf']['total']}", flush=True)
            except Exception as e:  # noqa: BLE001
                vol_rep["pdf"] = {"error": str(e)[:200]}
                print(f"[m5] {src.name}: PDF-аннотации не удались: {e}", flush=True)
        report["volumes"].append(vol_rep)
        print(f"[m5] {src.name}: замен {stats['replace']}, вставок {stats['insert']}, "
              f"вручную {stats['manual']}, приложений зарезервировано "
              f"{stats.get('reserved', 0)} → {out.name}", flush=True)
    if failed and not outs:
        raise RuntimeError("Ни один том не удалось открыть: " + "; ".join(failed)
                           + ". Откройте файлы в Word и пересохраните как .docx.")
    try:
        report["report_docx"] = str(_write_report(project, report))
    except Exception as e:  # noqa: BLE001
        print(f"[m5] отчёт финал-проверки не записан: {e}", flush=True)
    return outs, failed
