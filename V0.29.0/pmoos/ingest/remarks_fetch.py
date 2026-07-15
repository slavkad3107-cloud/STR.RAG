"""Скачивание файла замечаний по ссылке (https, включая Google Drive «по ссылке»).

Вынесено из app/hub.py: сетевые запросы и разбор ссылок — не-презентационная
логика, ей место в pmoos/ (UI остаётся тонкой обёрткой)."""
from __future__ import annotations

import re
from pathlib import Path

from ..paths import project_paths


def gdrive_direct(url: str):
    """Преобразует ссылку Google Drive (file/d/…, open?id=, uc?id=) в прямую."""
    m = re.search(r"drive\.google\.com/(?:file/d/([-\w]{10,})|open\?id=([-\w]{10,})"
                  r"|uc\?[^\s]*?id=([-\w]{10,}))", url)
    if m:
        fid = next(g for g in m.groups() if g)
        return f"https://drive.google.com/uc?export=download&id={fid}", fid
    return url, None


def download_remarks_url(project: str, url: str) -> Path:
    """Скачивает файл замечаний по ссылке в постоянную папку remarks/ проекта."""
    import requests
    from urllib.parse import urlparse, unquote
    direct, fid = gdrive_direct(url.strip())
    r = requests.get(direct, timeout=90, allow_redirects=True)
    if fid and "text/html" in (r.headers.get("Content-Type") or ""):
        m = re.search(r"confirm=([0-9A-Za-z_\-]+)", r.text)
        if m:  # большие файлы Drive требуют подтверждения
            r = requests.get(f"{direct}&confirm={m.group(1)}", timeout=180,
                             allow_redirects=True)
    r.raise_for_status()
    ctype = (r.headers.get("Content-Type") or "").lower()
    if "text/html" in ctype:
        raise RuntimeError("по ссылке вернулась HTML-страница, а не файл. Для Google Drive "
                           "включите доступ «Все, у кого есть ссылка» и давайте ссылку на "
                           "ФАЙЛ (не на папку).")
    name = None
    cd = r.headers.get("Content-Disposition") or ""
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
    if m:
        name = unquote(m.group(1)).strip()
    if not name:
        name = Path(urlparse(direct).path).name or "замечания_по_ссылке"
        if "." not in Path(name).name:
            name += (".pdf" if "pdf" in ctype else
                     ".docx" if "wordprocessingml" in ctype else
                     ".doc" if "msword" in ctype else
                     ".xlsx" if "spreadsheetml" in ctype else ".bin")
    rdir = project_paths(project)["remarks_dir"]
    rdir.mkdir(parents=True, exist_ok=True)
    out = rdir / name
    out.write_bytes(r.content)
    if out.stat().st_size < 64:
        raise RuntimeError(f"скачан подозрительно маленький файл ({out.stat().st_size} байт) — "
                           f"проверьте доступ по ссылке.")
    return out
