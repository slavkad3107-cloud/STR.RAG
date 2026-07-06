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


# ─── семантический чанкинг (v0.23, opt-in) ──────────────────────────────────
# По вердикту dex-дебата: главный выигрыш качества для нормативных документов РФ
# — резать НЕ по символам, а по СМЫСЛОВЫМ границам (пункты НПА, заголовки,
# абзацы), собирая цельные положения до ~512 токенов bge-m3. Целевой размер в
# ТОКЕНАХ (а не символах): 1200 символов рус. ≈ 360 токенов, лимит модели 1024 —
# значит можно брать крупнее, но именно по границам пунктов, а не посреди фразы.
_BOUNDARY_NUM = re.compile(r"^\d+(?:\.\d+){0,4}[.)]?(?:\s|$)")
_BOUNDARY_KW = re.compile(
    r"^(?:п\.?\s*\d|пункт\s+\d|подпункт|раздел\b|глава\b|приложение\b|"
    r"таблица\b|рисунок\b|рис\.|статья\s+\d|part\b)", re.IGNORECASE)


def _is_boundary_line(line: str) -> bool:
    """Строка начинает НОВЫЙ смысловой блок (пункт НПА / заголовок)?"""
    s = line.strip()
    if not s:
        return False
    if _BOUNDARY_NUM.match(s) or _BOUNDARY_KW.match(s):
        return True
    # заголовок КАПСОМ отдельной строкой (короткий, буквы почти все заглавные)
    letters = [c for c in s if c.isalpha()]
    if letters and 6 <= len(s) <= 90 and sum(c.isupper() for c in letters) / len(letters) > 0.8:
        return True
    return False


def _split_sentences(block: str, cap: int) -> list[str]:
    """Блок крупнее cap → делим по предложениям (крайний случай — по символам),
    чтобы ни один чанк не превышал лимит модели."""
    sents = re.split(r"(?<=[.!?;])\s+", block)
    out, buf = [], ""
    for s in sents:
        if buf and len(buf) + 1 + len(s) > cap:
            out.append(buf)
            buf = s
        else:
            buf = (buf + " " + s) if buf else s
    if buf:
        out.append(buf)
    final: list[str] = []
    for x in out:
        if len(x) <= cap:
            final.append(x)
        else:  # одно сверх-длинное предложение — режем жёстко
            final.extend(x[i:i + cap] for i in range(0, len(x), cap))
    return final


def chunk_text_semantic(text: str, *, target_tokens: int = 512,
                        chars_per_token: float = 3.2, min_chunk: int = 80) -> list[str]:
    """Нарезка по смысловым границам, цель ~target_tokens токенов на чанк.

    Границы: нормативная нумерация («5.4.», «3.1.2)»), «п./пункт/раздел/таблица/
    приложение», заголовки капсом, абзацы. Блоки жадно упаковываются до целевого
    размера; сверх-крупные блоки дробятся по предложениям. Перекрытия нет —
    границы уже чистые (в отличие от посимвольного режима)."""
    text = normalize_text(text)
    if not text:
        return []
    target_chars = max(min_chunk, int(target_tokens * chars_per_token))
    hard_cap = int(target_chars * 1.4)

    # 1) блоки по смысловым границам (каждая строка — ровно в один блок)
    blocks: list[str] = []
    cur: list[str] = []
    for line in text.split("\n"):
        if cur and _is_boundary_line(line):
            blocks.append("\n".join(cur))
            cur = [line]
        else:
            cur.append(line)
    if cur:
        blocks.append("\n".join(cur))

    # 2) сверх-крупные блоки — по предложениям
    units: list[str] = []
    for b in blocks:
        units.extend([b] if len(b) <= hard_cap else _split_sentences(b, hard_cap))

    # 3) жадная упаковка блоков в чанки до целевого размера
    chunks: list[str] = []
    buf = ""
    for u in units:
        if buf and len(buf) + 1 + len(u) > target_chars:
            chunks.append(buf.strip())
            buf = u
        else:
            buf = (buf + "\n" + u) if buf else u
    if buf.strip():
        chunks.append(buf.strip())
    res = [c for c in chunks if len(c) >= min_chunk]
    return res or ([text] if len(text) >= min_chunk else [])


def _page_number(loc: str) -> int | None:
    """Извлечь номер страницы/листа из строки loc ('стр. 45', 'Лист 3' → 45/3)."""
    m = re.search(r"\d+", loc or "")
    return int(m.group(0)) if m else None


def build_chunks(*, project: str, file_rel: str, section_code: str,
                 doc_sha: str, pages: list[dict], size: int = 1200,
                 overlap: int = 200, min_chunk: int = 80,
                 doc_type: str | None = None, mode: str = "char",
                 target_tokens: int = 512, chars_per_token: float = 3.2) -> list[dict]:
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
        # Таблицы НЕ режем по смыслу (их собирает merge_tables при поиске) —
        # семантический режим применяем только к сплошному тексту.
        if mode == "semantic" and not is_table:
            pieces = chunk_text_semantic(page.get("text", ""), target_tokens=target_tokens,
                                         chars_per_token=chars_per_token, min_chunk=min_chunk)
        else:
            pieces = chunk_text(page.get("text", ""), size=size, overlap=overlap, min_chunk=min_chunk)
        for piece in pieces:
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
