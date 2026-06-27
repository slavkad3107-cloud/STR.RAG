"""Нарезка текста на чанки со СТАБИЛЬНЫМИ идентификаторами.

Исправляет замечание всех ревью: раньше id = uuid.uuid4() менялся при каждой
переиндексации, из-за чего нельзя было сравнивать версии и удалять конкретные
чанки. Теперь id детерминирован: UUID из sha1(file|loc|index|text).
Qdrant требует UUID/целое — детерминированный UUID идеально подходит.
"""
from __future__ import annotations

import hashlib
import re
import uuid


def deterministic_id(*parts: str) -> str:
    key = "||".join(parts)
    digest = hashlib.sha1(key.encode("utf-8")).digest()[:16]
    return str(uuid.UUID(bytes=digest))


def normalize_text(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text or "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(text: str, *, size: int = 1200, overlap: int = 200,
               min_chunk: int = 80) -> list[str]:
    """Нарезка по символам с перекрытием. Стараемся резать по границам абзацев."""
    text = normalize_text(text)
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        # пытаемся не рвать предложение/абзац — ищем ближайший разрыв назад
        if end < n:
            window = text[start:end]
            cut = max(window.rfind("\n"), window.rfind(". "), window.rfind("; "))
            if cut > size * 0.5:
                end = start + cut + 1
        piece = text[start:end].strip()
        if len(piece) >= min_chunk:
            chunks.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks


def _page_number(loc: str) -> int | None:
    """Извлечь номер страницы/листа из строки loc ('стр. 45', 'Лист 3' → 45/3)."""
    m = re.search(r"\d+", loc or "")
    return int(m.group(0)) if m else None


def build_chunks(*, project: str, file_rel: str, section_code: str,
                 doc_sha: str, pages: list[dict], size: int = 1200,
                 overlap: int = 200, min_chunk: int = 80,
                 doc_type: str | None = None) -> list[dict]:
    """Собирает чанки со всеми метаданными (payload) для индексации.

    pages — результат loaders.extract_file (список единиц с loc/text/is_table).
    В payload добавлены метаданные (по требованию ревью и пользователя):
      doc_type      — тип документа (ПМООС/ПОС/ТКР… по разделу);
      section       — код раздела; section_num — номер по ПП-87 (5.4, 8 и т.п.);
      normative_ref — нормативы (СП/ГОСТ/СанПиН…), упомянутые в чанке;
      page_number   — номер страницы/листа; file_name — имя файла;
      is_table, doc_sha, chunk_index — как раньше.
    """
    from .sections import section_short, section_num
    from ..normatives.engine import find_references
    from pathlib import PurePath

    file_name = PurePath(file_rel).name
    sec_num = section_num(section_code)
    if not doc_type:
        doc_type = section_short(section_code) if section_code and section_code != "UNKNOWN" else "—"

    out: list[dict] = []
    idx = 0
    for page in pages:
        loc = page.get("loc", "")
        is_table = bool(page.get("is_table"))
        page_no = _page_number(loc)
        for piece in chunk_text(page.get("text", ""), size=size, overlap=overlap, min_chunk=min_chunk):
            cid = deterministic_id(file_rel, loc, str(idx), piece)
            try:
                norm_refs = find_references(piece)
            except Exception:  # noqa: BLE001
                norm_refs = []
            out.append({
                "id": cid,
                "text": piece,
                "payload": {
                    "project": project,
                    "file": file_rel,
                    "file_name": file_name,
                    "loc": loc,
                    "page_number": page_no,
                    "section": section_code,
                    "section_num": sec_num,
                    "doc_type": doc_type,
                    "normative_ref": norm_refs,
                    "is_table": is_table,
                    "doc_sha": doc_sha,
                    "chunk_index": idx,
                },
            })
            idx += 1
    return out
