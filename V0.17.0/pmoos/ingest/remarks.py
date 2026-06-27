"""Загрузка замечаний экспертизы из файла.

Источник — таблица в DOCX/XLSX (как «замечания оос_1-75.docx») либо произвольный
текст (тогда разбираем через ИИ). Раньше нетабличный разбор падал с «Не удалось
извлечь сбалансированный JSON» — теперь используется устойчивый chat_json.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from pathlib import Path

from ..config import Config


@dataclass
class Remark:
    number: str
    text: str
    category: str = ""        # тематика (шум/выбросы/отходы/СЗЗ/вода/…)
    source_hint: str = ""     # подсказка раздела-источника, если есть

    def to_dict(self) -> dict:
        return asdict(self)


_HEADER_HINTS = ("замечан", "п/п", "№", "номер", "содержан", "текст")


def _looks_like_header(cells: list[str]) -> bool:
    joined = " ".join(c.lower() for c in cells)
    return any(h in joined for h in _HEADER_HINTS)


def _remarks_from_rows(rows: list[list[str]]) -> list[Remark]:
    """Из строк таблицы: номер — первый короткий числовой столбец, текст —
    самый «длинный» столбец."""
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        return []
    start = 1 if _looks_like_header(rows[0]) else 0
    body = rows[start:]
    if not body:
        return []
    ncols = max(len(r) for r in body)
    # индекс столбца с самым длинным средним текстом = текст замечания
    avg_len = [0.0] * ncols
    for r in body:
        for i in range(ncols):
            if i < len(r):
                avg_len[i] += len(r[i] or "")
    text_col = max(range(ncols), key=lambda i: avg_len[i])
    out: list[Remark] = []
    for i, r in enumerate(body, start=1):
        text = (r[text_col] if text_col < len(r) else "").strip()
        if len(text) < 8:
            continue
        # номер: первый столбец, если он короткий/числовой, иначе порядковый
        num = ""
        if r and r[0] and text_col != 0:
            cand = r[0].strip()
            if len(cand) <= 12:
                num = cand
        out.append(Remark(number=num or str(i), text=text))
    return out


def _docx_tables_rows(path: Path) -> list[list[list[str]]]:
    import docx
    d = docx.Document(str(path))
    tables = []
    for t in d.tables:
        rows = [[(c.text or "").strip() for c in row.cells] for row in t.rows]
        tables.append(rows)
    return tables


def _xlsx_rows(path: Path) -> list[list[str]]:
    import openpyxl
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    ws = wb.active
    rows = [[("" if c is None else str(c)) for c in row]
            for row in ws.iter_rows(values_only=True)]
    wb.close()
    return rows


def _ai_extract(text: str, cfg: Config) -> list[Remark]:
    from ..core.ai_providers import chat_json
    sys = ("Ты извлекаешь замечания экспертизы из текста. Верни ТОЛЬКО JSON-массив "
           "объектов вида {\"number\": \"<номер>\", \"text\": \"<полный текст замечания>\"}. "
           "Сохрани нумерацию из текста. Никаких пояснений.")
    data = chat_json(cfg, [{"role": "system", "content": sys},
                           {"role": "user", "content": text[:60000]}],
                     role="extract", expect="array")
    out: list[Remark] = []
    if isinstance(data, list):
        for i, item in enumerate(data, start=1):
            if isinstance(item, dict) and item.get("text"):
                out.append(Remark(number=str(item.get("number") or i),
                                  text=str(item["text"]).strip()))
    return out


_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _is_ole(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(8) == _OLE_MAGIC
    except OSError:
        return False


def _convert_doc_with_word(path: Path) -> Path:
    """Старый бинарный .doc → .docx через установленный Microsoft Word (COM).
    Возвращает путь к сконвертированному файлу (рядом, *.converted.docx)."""
    out = path.with_suffix(".converted.docx")
    if out.exists() and out.stat().st_size > 0:
        return out
    try:
        import win32com.client  # pywin32 уже стоит на Windows (зависимость qdrant)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"«{path.name}» — файл старого формата .doc. Для автоматической конвертации "
            f"нужен установленный Microsoft Word. Либо откройте файл в Word и "
            f"пересохраните как .docx, затем загрузите заново.") from e
    word = None
    try:
        word = win32com.client.Dispatch("Word.Application")
        word.Visible = False
        doc = word.Documents.Open(str(path))
        doc.SaveAs2(str(out), FileFormat=16)  # 16 = wdFormatXMLDocument (.docx)
        doc.Close(False)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"Не удалось сконвертировать «{path.name}» через Microsoft Word: {e}. "
            f"Откройте файл в Word и пересохраните как .docx.") from e
    finally:
        try:
            if word is not None:
                word.Quit()
        except Exception:  # noqa: BLE001
            pass
    print(f"[remarks] {path.name}: .doc сконвертирован через Word → {out.name}", flush=True)
    return out


def load_remarks(path: Path, cfg: Config) -> list[Remark]:
    """Главная функция: пытается таблицей, при неудаче — через ИИ."""
    ext = path.suffix.lower()
    # старый бинарный .doc (или .doc, переименованный в .docx) → конвертация через Word
    if ext == ".doc" or (ext == ".docx" and _is_ole(path)):
        path = _convert_doc_with_word(path)
        ext = ".docx"
    remarks: list[Remark] = []
    if ext == ".docx":
        best: list[Remark] = []
        for rows in _docx_tables_rows(path):
            cand = _remarks_from_rows(rows)
            if len(cand) > len(best):
                best = cand
        remarks = best
    elif ext in (".xlsx", ".xlsm"):
        remarks = _remarks_from_rows(_xlsx_rows(path))

    if remarks:
        return remarks

    # fallback: вытащить весь текст и разобрать через ИИ
    from .loaders import extract_file
    pages = extract_file(path, ocr=bool(cfg.get("ocr.enabled", True)),
                         min_text_chars=int(cfg.get("ocr.min_text_chars", 200)),
                         lang=cfg.get("ocr.lang", "rus+eng"))
    full = "\n".join(p["text"] for p in pages)
    # пробуем простую нумерацию "1." / "1)" перед ИИ
    simple = _split_numbered(full)
    if len(simple) >= 3:
        return simple
    return _ai_extract(full, cfg)


# Начало пункта: «12.», «12)» в начале строки, либо номер ОДИН на строке
# (так PDF часто отдаёт колонку «№» из таблиц).
_NUM_START = re.compile(r"(?m)^\s*(\d{1,3})\s*(?:[.)]\s+|\s*$)")


def _split_numbered(text: str) -> list[Remark]:
    """Детерминированный разбор нумерованного списка с МНОГОСТРОЧНЫМИ пунктами.

    Раньше бралась только первая строка каждого пункта — поэтому замечания из
    PDF уходили в ИИ-фолбэк и падали на невалидном JSON. Теперь текст режется
    по началам номеров, а тело пункта — всё до следующего номера.
    """
    text = text or ""
    starts = [(m.start(), m.end(), m.group(1)) for m in _NUM_START.finditer(text)]
    if not starts:
        return []
    # защита от ложных срабатываний (числа в тексте): номера должны в целом
    # возрастать; «провалы» назад отбрасываем (кроме перезапуска с 1)
    filt: list[tuple[int, int, str]] = []
    prev = 0
    for s, e, num in starts:
        n = int(num)
        if n >= prev or n == 1:
            filt.append((s, e, num))
            prev = n
    out: list[Remark] = []
    for i, (s, e, num) in enumerate(filt):
        end = filt[i + 1][0] if i + 1 < len(filt) else len(text)
        body = re.sub(r"\s*\n\s*", " ", text[e:end]).strip()
        if len(body) >= 10:
            out.append(Remark(number=num, text=body))
    return out
