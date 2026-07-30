"""Менеджер версий разделов ПД (МОДУЛЬ 1, требование «таблица + граф изменений с датами»).

Один и тот же раздел приходит много раз: исходный, по замечаниям, финальный.
Группируем версии по «базовому имени» (без маркеров корр/изм/v2/финал и дат) и
по сходству содержимого. Для каждой версии храним дату (из имени файла, иначе
mtime) и метку (исходная/откорректированная/актуальная).

Результат пишем в project/versions.json и отдаём данные для визуального графа
изменений во времени.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ..paths import project_paths
from ..ingest.sections import classify_filename, detect_version_hint
from ..ingest.dedup import simhash_similarity, minhash_similarity


def _content_sim(a: dict, b: dict) -> float:
    """Сходство содержимого двух подписей: sha256→1.0; иначе MinHash; иначе SimHash."""
    if a.get("sha256") and a.get("sha256") == b.get("sha256"):
        return 1.0
    if a.get("minhash") and b.get("minhash"):
        return minhash_similarity(a["minhash"], b["minhash"])
    if a.get("simhash") is not None and b.get("simhash") is not None:
        return simhash_similarity(a["simhash"], b["simhash"])
    return 0.0


# ───────── контентные подписи (пункт 4: различать версии по содержимому) ─────────
def _content_sigs_path(project: str) -> Path:
    return project_paths(project)["root"] / "content_sigs.json"


def load_content_sigs(project: str) -> dict[str, dict]:
    p = _content_sigs_path(project)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_content_sig(project: str, file_name: str, sig: dict) -> None:
    """Сохранить подпись содержимого файла (вызывается индексатором). Файлы не
    хранятся — только подпись {sha256, simhash, n_chars}."""
    p = _content_sigs_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = load_content_sigs(project)
    data[file_name] = sig
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _apply_content_analysis(project: str, groups: dict[str, dict]) -> list[str]:
    """Сверить версии по содержимому. Возвращает предупреждения и аннотирует группы.

    • в группе (один раздел/имя) версии с НИЗКИМ сходством содержимого помечаются
      «похожее имя, но разное содержимое»;
    • файлы из РАЗНЫХ групп с почти одинаковым содержимым → «дубликат под другим
      именем» (sha256 совпал или SimHash-сходство высокое)."""
    sigs = load_content_sigs(project)
    if not sigs:
        return []
    warnings: list[str] = []
    DUP, DIFF = 0.85, 0.40  # пороги сходства (MinHash≈Жаккар)

    # внутри групп: расхождение содержимого у «версий»
    for g in groups.values():
        files = [v["file"] for v in g["versions"] if v["file"] in sigs]
        for i in range(len(files)):
            for j in range(i + 1, len(files)):
                sim = _content_sim(sigs[files[i]], sigs[files[j]])
                if sim < DIFF:
                    note = (f"⚠ «{files[i]}» и «{files[j]}» похожи по имени, "
                            f"но содержимое разное (сходство {sim:.0%}) — возможно, это разные документы")
                    g.setdefault("content_notes", []).append(note)
                    warnings.append(note)

    # между группами: одинаковое содержимое под разными именами
    flat = [(g["versions"], gk) for gk, g in groups.items()]
    all_files = [(v["file"], sigs[v["file"]]) for vers, _ in flat for v in vers if v["file"] in sigs]
    for i in range(len(all_files)):
        for j in range(i + 1, len(all_files)):
            (fa, sa), (fb, sb) = all_files[i], all_files[j]
            if fa == fb:
                continue
            sim = _content_sim(sa, sb)
            if sim >= DUP:
                warnings.append(f"ℹ «{fa}» и «{fb}» — почти идентичное содержимое "
                                f"({sim:.0%}); вероятно дубликат под другим именем")
    return warnings


# Слова-маркеры версий и их «вес» (чем больше — тем новее).
_RANK_WORDS: list[tuple[tuple[str, ...], int, str]] = [
    (("исходн", "первичн", "orig", "original"), 0, "исходная"),
    (("черновик", "draft"), 1, "черновик"),
    (("корр", "корректир", "изм", "испр", "revised", "rev", "правк", "ред", "редакц"), 2, "откорректированная"),
    (("финал", "итог", "final", "актуальн", "действ"), 3, "актуальная"),
]
_VNUM_JOIN = re.compile(r"^(?:[vв]|изм|ред|версия|вер)\.?(\d{1,2})$", re.I)
_VMARK_TOK = {"v", "в", "изм", "ред", "версия", "вер"}


def _version_rank(filename: str) -> tuple[int, str]:
    stem = Path(filename).stem
    tokens = [t.lower() for t in re.split(r"[\s_\-.]+", stem) if t]
    rank, label = 0, "исходная"
    for t in tokens:
        for prefixes, r, lbl in _RANK_WORDS:
            if any(t.startswith(p) for p in prefixes) and r >= rank:
                rank, label = r, lbl
    # явная нумерация версий (v2, изм2, ред.3, либо «v» «2» раздельно)
    explicit = 0
    for i, t in enumerate(tokens):
        m = _VNUM_JOIN.match(t)
        if m:
            explicit = max(explicit, int(m.group(1)))
        elif t in _VMARK_TOK and i + 1 < len(tokens) and tokens[i + 1].isdigit():
            explicit = max(explicit, int(tokens[i + 1]))
    if explicit:
        rank = max(rank, 2) + explicit
    return rank, label


_DATE_PATTS = [
    (re.compile(r"(20\d{2})[._-](\d{2})[._-](\d{2})"), "%Y-%m-%d"),
    (re.compile(r"(\d{2})[._-](\d{2})[._-](20\d{2})"), "%d-%m-%Y"),
    (re.compile(r"(\d{2})\.(\d{2})\.(20\d{2})"), "%d-%m-%Y"),
]

# Полные даты и одиночный год — удаляются из базового имени.
_DATE_FULL_A = re.compile(r"20\d{2}[._\-]\d{1,2}[._\-]\d{1,2}")
_DATE_FULL_B = re.compile(r"\d{1,2}[._\-]\d{1,2}[._\-]20\d{2}")
_YEAR = re.compile(r"20\d{2}")
# Токен — слово-маркер версии (исходн/корр/финал/изм/…), без цифр.
_MARK_WORD_TOK = re.compile(
    r"^(?:исходн|первичн|orig|original|корр|корректир|изм|испр|revised|rev|правк|"
    r"финал|итог|final|актуальн|действ|draft|черновик|редакц|ред|версия|вер)[а-яёa-z]*$",
    re.I,
)
# Токен «маркер+номер» слитно: v2, в3, изм2, ред3.
_MARK_NUM_TOK = re.compile(r"^(?:[vв]|изм|ред|rev|версия|вер)\d{1,2}$", re.I)
_PURE_NUM = re.compile(r"^\d{1,2}$")


def _base_name(filename: str) -> str:
    """Базовое имя раздела без маркеров версий и дат.

    НЕ удаляем номера томов («том 6.1» vs «том 6.2» — разные тома, разные
    группы), но удаляем «изм.2», «v3», «корр», «финал», даты — это версии
    ОДНОГО документа. Работаем по токенам, т.к. подчёркивание ломает \\b.
    """
    stem = Path(filename).stem
    stem = _DATE_FULL_A.sub(" ", stem)
    stem = _DATE_FULL_B.sub(" ", stem)
    stem = _YEAR.sub(" ", stem)
    tokens = re.split(r"[\s_\-.]+", stem)
    out: list[str] = []
    skip_num = False
    for t in tokens:
        if not t:
            continue
        tl = t.lower()
        if _MARK_WORD_TOK.match(tl):
            skip_num = True            # съесть номер версии, если идёт следом
            continue
        if _MARK_NUM_TOK.match(tl):
            skip_num = False
            continue
        if skip_num and _PURE_NUM.match(tl):
            skip_num = False           # это был номер версии (изм 2)
            continue
        skip_num = False
        out.append(tl)
    return " ".join(out) or stem.lower()


def _date_from_name(filename: str) -> str | None:
    for patt, fmt in _DATE_PATTS:
        m = patt.search(filename)
        if m:
            try:
                if fmt == "%Y-%m-%d":
                    dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                else:
                    dt = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
                return dt.date().isoformat()
            except Exception:
                continue
    return None


def analyze_versions(project: str, *, object_type: str = "площадной") -> dict[str, Any]:
    """Группирует файлы из tmp_uploads по разделам и версиям.

    Файлы не сохраняются (требование пользователя): читаем только имена и mtime.
    """
    paths = project_paths(project)
    up = paths["uploads"]
    groups: dict[str, dict[str, Any]] = {}
    if up.exists():
        for fp in sorted(up.rglob("*")):
            if not fp.is_file():
                continue
            name = fp.name
            cls = classify_filename(name, object_type, top=1)
            section = cls[0]["code"] if cls else "UNKNOWN"
            base = _base_name(name)
            gkey = f"{section}::{base}"
            rank, label = _version_rank(name)
            date = _date_from_name(name)
            if not date:
                try:
                    date = datetime.fromtimestamp(fp.stat().st_mtime).date().isoformat()
                except Exception:
                    date = None
            g = groups.setdefault(gkey, {"section": section, "base": base, "versions": []})
            g["versions"].append({
                "file": name, "rank": rank, "label": label,
                "date": date, "version_hint": detect_version_hint(name) or "",
            })

    # пометить актуальную версию в каждой группе
    for g in groups.values():
        vers = sorted(g["versions"], key=lambda v: (v["rank"], v.get("date") or ""))
        for v in vers:
            v["is_current"] = False
        if vers:
            vers[-1]["is_current"] = True
            vers[-1]["label"] = vers[-1]["label"] if vers[-1]["label"] != "исходная" else "актуальная"
        g["versions"] = vers
        g["current_file"] = vers[-1]["file"] if vers else None

    # сверка версий по содержимому (если есть подписи после индексации)
    content_warnings = _apply_content_analysis(project, groups)

    result = {"project": project, "object_type": object_type, "groups": groups,
              "content_warnings": content_warnings,
              "generated_at": datetime.now().isoformat(timespec="seconds")}
    out = paths["versions"]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def set_current_version(project: str, group_key: str, filename: str) -> dict:
    """Пользователь вручную выбирает актуальную версию (требование: дать выбрать)."""
    paths = project_paths(project)
    data = json.loads(paths["versions"].read_text(encoding="utf-8"))
    g = data["groups"].get(group_key)
    if g:
        for v in g["versions"]:
            v["is_current"] = (v["file"] == filename)
        g["current_file"] = filename
        paths["versions"].write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def change_timeline(project: str) -> list[dict]:
    """Плоский список событий версий для графа изменений во времени."""
    paths = project_paths(project)
    if not paths["versions"].exists():
        return []
    data = json.loads(paths["versions"].read_text(encoding="utf-8"))
    events = []
    for gkey, g in data.get("groups", {}).items():
        for v in g["versions"]:
            events.append({
                "group": gkey, "section": g["section"], "file": v["file"],
                "date": v.get("date"), "label": v["label"],
                "is_current": v.get("is_current", False),
            })
    events.sort(key=lambda e: (e.get("date") or "", e["section"]))
    return events
