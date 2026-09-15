# -*- coding: utf-8 -*-
"""Тома и пусковые комплексы (v0.55 «Опочка», 15.09.2026).

Итог проверки на ОПОЧКЕ (тестировщик по 75 замечаниям): программа путала
том-адресат и пусковой комплекс (ПК) — числа I ПК (82 чел., 7,3 км) уезжали
во все три тома ООС, смежные тома чужого ПК шли в источники, «было» было
пересказом замечания. Здесь — детерминированные помощники:

  * volume_token / pk_of — номер тома из имени файла и индекс ПК (последняя
    компонента номера тома: 6.2 → ПК 2; 5.1.1.1 → ПК 1; 3.4.3 → ПК 3);
  * oos_volumes / target_volumes / ref_volumes — какие тома ООС адресует
    замечание («Том 6.2, п. 3.5.2»; «Раздел 6» без тома → все тома) и какие
    смежные тома названы в «Основании» («Том 3.1.2, лист ТКР.АД-ВР19»);
  * pk_filter — фрагменты чужого ПК убираются (ООС) или уходят в конец;
  * verify_was — контракт «было»: цитата обязана встречаться в томе-адресате
    (нормализация + покрытие 3-граммами ≥ 0.85), иначе «место не подтверждено»;
  * unsupported_requisites — реквизиты (номера лицензий/договоров/писем,
    названия ООО, даты), которых нет во фрагментах, — признак выдумки;
  * passport — паспорт проекта по ПК из реестра показателей (по источникам).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

VOL_RX = re.compile(r"том[аеу]?\s*№?\s*(\d+(?:\.\d+)+)", re.I)
_ROMAN = {"I": "1", "II": "2", "III": "3", "IV": "4", "V": "5"}


def volume_token(name: str) -> str:
    """«Раздел ПД №6_ООС_том 6.1.pdf» → '6.1'; «Том 5.1.1.1_717-…-ПОС1.pdf» → '5.1.1.1'."""
    stem = Path(str(name)).stem
    m = VOL_RX.search(stem)
    if m:
        return m.group(1)
    m = re.search(r"(?<![\d.])(\d{1,2}\.\d+(?:\.\d+)*)(?![\d.])", stem)
    return m.group(1) if m else ""


def pk_of(token: str) -> str | None:
    """Индекс пускового комплекса — последняя компонента номера тома
    (только для многокомпонентных номеров; '10.1' → '1', '6' → None)."""
    if not token or "." not in token:
        return None
    return token.split(".")[-1]


def _state_files(project: str) -> dict[str, dict]:
    from ..paths import project_paths
    p = project_paths(project)["index_state"]
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("files") or {}
    except Exception:  # noqa: BLE001
        return {}


def oos_volumes(project: str, target: str = "OOS") -> dict[str, str]:
    """{'6.1': 'Раздел ПД №6_ООС_том 6.1.pdf', …} — тома целевого раздела в базе."""
    out: dict[str, str] = {}
    for rel, fi in _state_files(project).items():
        name = Path(rel).name
        sec = (fi or {}).get("section", "")
        if sec == target or (target == "OOS" and re.search(r"оос|пмоос|мооос", name, re.I)):
            tok = volume_token(name)
            if tok:
                out[tok] = name
    return dict(sorted(out.items()))


def target_volumes(remark_text: str, oos_map: dict[str, str]) -> list[str]:
    """Тома ООС, к которым относится замечание. Названы явно — только они;
    иначе (общее по разделу) — все тома."""
    if not oos_map:
        return []
    text = remark_text or ""
    found = [t for t in re.findall(r"том[аеу]?\s*№?\s*(\d+(?:\.\d+)+)", text, re.I) if t in oos_map]
    # «I пусковой комплекс» → том с ПК 1
    for rom, num in _ROMAN.items():
        if re.search(rf"\b{rom}\s+пусков", text):
            for t in oos_map:
                if pk_of(t) == num and t not in found:
                    found.append(t)
    if found:
        return list(dict.fromkeys(found))
    return list(oos_map)


def ref_volumes(remark_text: str, oos_map: dict[str, str]) -> list[str]:
    """Смежные тома, названные в замечании/основании (не тома ООС)."""
    toks = re.findall(r"том[аеу]?\s*№?\s*(\d+(?:\.\d+)+)", remark_text or "", re.I)
    return list(dict.fromkeys(t for t in toks if t not in oos_map))


def pk_filter(hits: list[dict], pk: str | None, oos_files: set[str] | None = None) -> list[dict]:
    """Фрагменты ЧУЖОГО пускового комплекса: из томов ООС — убрать совсем,
    из смежных разделов — в конец списка (штраф). Без ПК — как есть."""
    if not pk:
        return list(hits)
    oos_files = oos_files or set()
    keep, tail = [], []
    for h in hits:
        f = (h.get("payload") or {}).get("file", "")
        hp = pk_of(volume_token(f))
        if hp is None or hp == pk:
            keep.append(h)
        elif f in oos_files:
            continue
        else:
            tail.append(h)
    return keep + tail


# ───────────── контракт «было» ─────────────
def normalize(s: str) -> str:
    s = (s or "").lower().replace("ё", "е").replace("­", "")
    s = re.sub(r"-\s*\n\s*", "", s)               # перенос по дефису
    s = re.sub(r"[‐-―−]", "-", s)   # все тире/дефисы → '-'
    s = s.replace(" ", " ")
    s = re.sub(r"[«»\"“”„']", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _grams(s: str, n: int = 3) -> set[tuple[str, ...]]:
    w = re.findall(r"[а-яa-z0-9]+", s)
    if len(w) < n:
        return {tuple(w)} if w else set()
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}


def _best_window(was_n: str, text: str) -> str:
    """Дословный фрагмент текста (по предложениям), покрывающий цитату."""
    sents = re.split(r"(?<=[.;:])\s+", text)
    gw = _grams(was_n)
    best, best_cov = "", 0.0
    for i in range(len(sents)):
        for j in range(i, min(len(sents), i + 3)):
            win = " ".join(sents[i:j + 1])
            cov = len(gw & _grams(normalize(win))) / max(1, len(gw))
            if cov > best_cov or (cov == best_cov and best and len(win) < len(best)):
                best, best_cov = win, cov
            if cov >= 0.999:
                break
    return best.strip()


def verify_was(was: str, hits: list[dict], threshold: float = 0.85) -> dict:
    """Проверить, что «было» действительно есть во фрагментах тома-адресата.
    Возвращает {verified, score, file, loc, quote} — quote = дословный фрагмент
    тома (им и надо заменить «было», если ИИ переставил слова)."""
    was_n = normalize(was)
    if len(was_n) < 12:
        return {"verified": False, "score": 0.0, "file": "", "loc": "", "quote": ""}
    gw = _grams(was_n)
    best = {"verified": False, "score": 0.0, "file": "", "loc": "", "quote": ""}
    for h in hits:
        text = h.get("text") or (h.get("payload") or {}).get("text") or ""
        tn = normalize(text)
        if not tn:
            continue
        if was_n in tn:
            score = 1.0
        else:
            score = len(gw & _grams(tn)) / max(1, len(gw))
        if score > best["score"]:
            pl = h.get("payload") or {}
            best = {"verified": score >= threshold, "score": round(score, 2),
                    "file": pl.get("file", ""), "loc": pl.get("loc", ""),
                    "quote": _best_window(was_n, text) if score >= threshold else ""}
        if score >= 0.999:
            break
    return best


# ───────────── выдуманные реквизиты ─────────────
_REQ_PATTERNS = [
    r"(?:лиценз\w*|договор\w*|письм\w*|справк\w*|заключен\w*|протокол\w*|акт\w*)\s*(?:№|N)\s*[\w\-/.]+",
    r"(?:ООО|АО|ПАО|ЗАО|МУП|ГУП|ФГБУ|ИП)\s*[«\"][^»\"]{2,60}[»\"]",
    r"\b\d{2}\.\d{2}\.(?:19|20)\d{2}\b",
]
_REQ_RX = [re.compile(p, re.I) for p in _REQ_PATTERNS]


def unsupported_requisites(text: str, context: str) -> list[str]:
    """Реквизиты в ответе, которых нет ни в одном фрагменте контекста."""
    ctx = normalize(context)
    ctx_digits = re.sub(r"\D", " ", ctx)
    out: list[str] = []
    for rx in _REQ_RX:
        for m in rx.finditer(text or ""):
            tok = m.group(0)
            tn = normalize(tok)
            digits = re.findall(r"\d{2,}", tn)
            if tn in ctx:
                continue
            if digits and all(f" {d} " in f" {ctx_digits} " for d in digits):
                continue
            # название организации: ищем по слову в кавычках
            name = re.search(r"[«\"]([^»\"]+)[»\"]", tok)
            if name and normalize(name.group(1)) in ctx:
                continue
            if tok not in out:
                out.append(tok)
    return out


# ───────────── паспорт проекта по ПК ─────────────
_PASSPORT_KEYS = ("length_route", "workers", "duration", "area_plot")
_PASSPORT_PREFER = ("POS", "TKR", "PPO", "PZU", "AR", "KR")


def passport(project: str) -> dict[str, dict]:
    """{'1': {'workers': {'value': '82', 'unit': 'чел.', 'file': …}, …}, '2': …}
    — значения показателей по пусковым комплексам (по источнику варианта)."""
    from ..data import registry as R
    inds = (R.load_registry(project).get("indicators") or {})
    out: dict[str, dict] = {}
    for key in _PASSPORT_KEYS:
        rec = inds.get(key) or {}
        best: dict[str, tuple[int, dict]] = {}
        for v in rec.get("variants") or []:
            for s in v.get("sources") or []:
                pk = pk_of(volume_token(s.get("file", "")))
                if not pk:
                    continue
                pri = _PASSPORT_PREFER.index(s.get("section")) if s.get("section") in _PASSPORT_PREFER else 9
                score = (pri, -int(v.get("count", 0)))
                cur = best.get(pk)
                if cur is None or score < cur[0]:
                    best[pk] = (score, {"value": v.get("value"), "unit": v.get("unit") or rec.get("unit", ""),
                                        "file": s.get("file", ""), "loc": s.get("loc", "")})
        for pk, (_, d) in best.items():
            out.setdefault(pk, {})[key] = d
    return dict(sorted(out.items()))


def passport_text(project: str, oos_map: dict[str, str] | None = None) -> str:
    from ..data import registry as R
    labels = {m["key"]: m["label"] for m in R.INDICATORS}
    pp = passport(project)
    if not pp:
        return ""
    oos_map = oos_map or {}
    lines = []
    for pk, vals in pp.items():
        vol = next((t for t in oos_map if pk_of(t) == pk), "")
        head = f"ПК {pk}" + (f" (том {vol})" if vol else "")
        parts = [f"{labels.get(k, k)}: {d['value']} {d['unit']} [{d['file']}, {d['loc']}]"
                 for k, d in vals.items()]
        lines.append(f"- {head}: " + "; ".join(parts))
    return "\n".join(lines)
