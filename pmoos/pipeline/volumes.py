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
    # дефис внутри слова («представле-ны» из PDF, скопированный ИИ) — убираем
    # с обеих сторон сравнения (санитарно-защитная → санитарнозащитная — симметрично)
    s = re.sub(r"(?<=[а-яa-z])-(?=[а-яa-z])", "", s)
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


def volume_texts(project: str) -> dict[str, str]:
    """Полный текст томов из corr_sources (*.docx, включая текстовые рамки) по
    номеру тома — для повторной проверки «было» по ВСЕМУ тому, а не по
    фрагментам поиска."""
    from ..paths import project_paths
    out: dict[str, str] = {}
    cdir = project_paths(project)["root"] / "corr_sources"
    if not cdir.exists():
        return out
    try:
        from docx import Document
        from ..output.docx_writer import _all_paragraphs
    except Exception:  # noqa: BLE001
        return out
    for f in sorted(cdir.glob("*.docx")):
        tok = volume_token(f.name)
        if not tok:
            continue
        try:
            d = Document(str(f))
            out[tok] = "\n".join(p.text for p in _all_paragraphs(d))
        except Exception:  # noqa: BLE001
            continue
    return out


def _sliced(text: str, size: int = 4000, step: int = 3000) -> list[dict]:
    """Текст тома окнами (как фрагменты) для verify_was."""
    return [{"text": text[i:i + size], "payload": {}} for i in range(0, max(1, len(text)), step)]


def reverify_answers(project: str, answers: list[dict] | None = None) -> dict:
    """ПОВТОРНАЯ ПРОВЕРКА «БЫЛО» по полному тексту томов (после загрузки томов во
    ВЫГРУЗКУ или после ответов): непроверенные цитаты, которые есть в томе,
    становятся подтверждёнными (с дословной подстановкой). Возвращает сводку;
    при answers=None читает и сохраняет answers.json."""
    from .block1_answers import load_answers, _save
    own = answers is None
    data = load_answers(project) if own else {"answers": answers}
    texts = volume_texts(project)
    if not texts:
        return {"checked": 0, "verified": 0, "note": "тома в corr_sources не загружены"}
    windows = {tok: _sliced(t) for tok, t in texts.items()}
    checked = verified = 0

    def _check(was: str, toks: list[str]) -> dict | None:
        best = None
        for tok in toks or list(windows):
            if tok not in windows:
                continue
            r = verify_was(was, windows[tok])
            if r["verified"] and (best is None or r["score"] > best["score"]):
                best = dict(r, volume=tok)
        return best

    def _candidates(was: str, toks: list[str], k: int = 3) -> list[dict]:
        """Ближайшие дословные фрагменты тома (покрытие ≥ 0.5) — предложить
        пользователю выбрать «было», если ИИ дал пересказ (вердикт dex: топ-3)."""
        found: list[dict] = []
        for tok in toks or list(windows):
            for w in windows.get(tok, []):
                r = verify_was(was, [w], threshold=0.5)
                if r["verified"] and r["quote"]:
                    found.append({"volume": tok, "score": r["score"], "quote": r["quote"][:500]})
        found.sort(key=lambda x: -x["score"])
        out, seen = [], set()
        for c in found:
            key = normalize(c["quote"])[:120]
            if key in seen:
                continue
            seen.add(key)
            out.append(c)
            if len(out) >= k:
                break
        return out
    for a in data.get("answers", []):
        toks = list(a.get("target_volumes") or [])
        if not a.get("was_verified"):
            was = (a.get("edit_was") or a.get("edit_was_unverified") or "").strip()
            if was:
                checked += 1
                r = _check(was, toks)
                if r:
                    a["edit_was"] = r["quote"] or was
                    a["was_verified"] = True
                    a["was_score"] = r["score"]
                    a["edit_was_unverified"] = ""
                    a["edit_was_candidates"] = []
                    a.setdefault("edit_was_src", {})
                    if not a["edit_was_src"]:
                        a["edit_was_src"] = {"file": texts and f"том {r['volume']}", "loc": "",
                                             "snippet": (r["quote"] or "")[:600], "score": r["score"]}
                    verified += 1
                else:
                    a["edit_was_candidates"] = _candidates(was, toks)
        for tok, ed in (a.get("volume_edits") or {}).items():
            if not isinstance(ed, dict) or ed.get("was_verified"):
                continue
            was = (ed.get("edit_was") or ed.get("edit_was_unverified") or "").strip()
            if was:
                checked += 1
                r = _check(was, [tok])
                if r:
                    ed["edit_was"] = r["quote"] or was
                    ed["was_verified"] = True
                    ed["edit_was_unverified"] = ""
                    verified += 1
    reflagged = _reflag(project, data.get("answers", []), texts)
    if own and (verified or reflagged["changed"]):
        _save(project, data)
    return {"checked": checked, "verified": verified, "volumes": sorted(texts),
            "reflagged": reflagged["changed"], "numbers_cleared": reflagged["cleared"],
            "numbers_added": reflagged["added"], "no_change": reflagged["no_change"]}


def _num_head(label: str) -> str:
    m = _NUM_TOK_RX.search(label or "")
    return re.sub(r"\s", "", m.group(0)).replace(",", ".") if m else (label or "")


def _reflag(project: str, answers: list[dict], texts: dict[str, str]) -> dict:
    """ПЕРЕПРОВЕРКА ФЛАГОВ готовых ответов без ИИ (раунд 4, тестировщик №4): числа
    «стало» сверяются с ПОЛНЫМ текстом тома-адресата + фрагментами-источниками +
    замечанием. Ложные тревоги (даты, ФККО, шифры, число есть в томе) снимаются,
    пропущенные выдумки «число + единица» добавляются; «стало» ≈ «было» → no_change."""
    try:
        from ..output.docx_writer import _same_text
    except Exception:  # noqa: BLE001
        _same_text = lambda a, b: False      # noqa: E731
    try:
        passport = passport_text(project, oos_volumes(project, "OOS"))
    except Exception:  # noqa: BLE001
        passport = ""
    changed = cleared = added = nochange = 0
    for a in answers:
        shall = (a.get("edit_shall") or "").strip()
        if not shall:
            continue
        toks = [t for t in (a.get("target_volumes") or []) if t in texts] or list(texts)
        ctx = "\n".join(texts[t] for t in toks) + "\n" + (a.get("remark") or "") + "\n" + passport + "\n" + \
            "\n".join(str(x.get("snippet") or "") for x in (a.get("sources") or []) + (a.get("retrieved_sources") or []))
        old = list(a.get("unsupported_numbers") or [])
        new = unsupported_numbers(shall, ctx)
        if sorted(_num_head(x) for x in old) != sorted(_num_head(x) for x in new):
            new_heads = {_num_head(x) for x in new}
            old_heads = {_num_head(x) for x in old}
            cleared += len(old_heads - new_heads)
            added += len(new_heads - old_heads)
            a["unsupported_numbers"] = new
            changed += 1
        nc = bool((a.get("edit_was") or "").strip()) and _same_text(shall, a.get("edit_was") or "")
        if nc and not a.get("no_change"):
            a["no_change"] = True
            nochange += 1
            changed += 1
        su = bool(a.get("unsupported_numbers")) and bool(a.get("requires_docs"))
        if su != bool(a.get("shall_unverified")):
            a["shall_unverified"] = su
            changed += 1
        if (a.get("unsupported_numbers") or a.get("no_change")) and a.get("confidence") == "high":
            a["confidence"] = "medium" if not a.get("no_change") else "low"
            changed += 1
    return {"changed": changed, "cleared": cleared, "added": added, "no_change": nochange}


# ───────────── выдуманные реквизиты ─────────────
_REQ_PATTERNS = [
    r"(?:лиценз\w*|договор\w*|письм\w*|справк\w*|заключен\w*|протокол\w*|акт\w*)\s*(?:№|N)\s*[\w\-/.‐-―]+",
    r"(?:ООО|АО|ПАО|ЗАО|МУП|ГУП|ФГБУ|ИП)\s*[«\"][^»\"]{2,60}[»\"]",
    r"\b\d{2}\.\d{2}\.(?:19|20)\d{2}\b",
]
_REQ_RX = [re.compile(p, re.I) for p in _REQ_PATTERNS]
# дата после названия НОРМАТИВА («ПП РФ от 16.02.2008 № 87») — не реквизит документа проекта
_NORM_BEFORE_RX = re.compile(r"(?:пп|постановлен\w*|приказ\w*|распоряжен\w*|фз|санпин|сп|гост|"
                             r"гн|сн|рд|методик\w*|закон\w*|кодекс\w*)[^\n]{0,60}$", re.I)


def unsupported_requisites(text: str, context: str) -> list[str]:
    """Реквизиты в ответе, которых нет ни в одном фрагменте контекста."""
    ctx = normalize(context)
    ctx_digits = re.sub(r"\D", " ", ctx)
    out: list[str] = []
    for rx in _REQ_RX:
        for m in rx.finditer(text or ""):
            tok = m.group(0)
            if "___" in tok or "__" in tok:
                continue                      # плейсхолдер «№ ___» — не выдумка
            if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", tok) and _NORM_BEFORE_RX.search(text[max(0, m.start() - 60):m.start()]):
                continue                      # дата норматива (ПП-87 от 16.02.2008)
            if re.search(r"(?:№|N)\s*[\w]{1,2}$", tok):
                continue                      # обрезанный «№ пр» — не судим
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


# ───────────── неподтверждённые ЧИСЛА в «стало» ─────────────
_NUM_TOK_RX = re.compile(r"(?<![\d.,])\d{1,3}(?:[  ]\d{3})*(?:[.,]\d+)?(?![\d.,])")


# не числа-значения: коды ФККО, даты, шифры, обозначения нормативов (тестировщик №4:
# ложное «31» из «31 мая 2023 г.» заблокировало верные правки №25 и №48)
_NOT_VALUE_RXS = [
    re.compile(r"\b\d\s\d{2}\s\d{3}\s\d{2}\s\d{2}\s\d\b"),                       # ФККО
    re.compile(r"\b\d{1,2}\s+(?:январ|феврал|март|апрел|ма[яй]|июн|июл|август|сентябр|октябр|ноябр|декабр)"
               r"\w*(?:\s+\d{4})?(?:\s*г(?:ода|\.)?)?", re.I),                          # 31 мая 2023 г.
    re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{2,4}\b"),                                      # 16.02.2008
    re.compile(r"\b(?:ГОСТ(?:\s*Р)?|СП|СНиП|СанПиН|СН|РД|ОДМ|ВСН|МУ|МР|ГН|ФЗ|ПП(?:\s*РФ)?|ИТС)"
               r"\s*№?\s*[\d][\d./\-–‑]*\d(?:\s*-\s*\d{2,4})?(?:-р|-ФЗ)?", re.I),   # СанПиН 1.2.3685-21
    re.compile(r"(?:№|N)\s*[\w./\-–‑]+", re.I),                                          # № 2409-р, № 717/14/15-П-1
    re.compile(r"\b\d+(?:[/\-–‑]\d+)+(?:[/\-–‑][А-ЯA-Zа-яa-z\d.]+)+"),                   # 117-24/С
    re.compile(r"\b(?:ст|стать\w+|п|пп|пункт\w*|табл\w*|таблиц\w*|рис\w*|прил\w*|приложени\w*|раздел\w*|глав\w*|"
               r"том\w*|лист\w*|стр|ИЗА|источник\w*\s*№?)\.?\s*№?\s*\d+(?:\.\d+)*", re.I),
]
_UNIT_FAMILIES = [
    ("mes", r"мес\w*"), ("mm", r"мм\b"), ("mg", r"мг\b"), ("m3", r"м[³3]|куб\w*\.?\s*м\w*"),
    ("m2", r"м[²2]|кв\.?\s*м\w*"), ("km", r"км\b|километр\w*"), ("ha", r"га\b|гектар\w*"),
    ("db", r"дб\s?а?\b"), ("kg", r"кг\b"), ("pct", r"%|процент\w*"), ("chel", r"чел\w*"),
    ("sht", r"шт\w*|штук\w*"), ("rub", r"руб\w*"), ("deg", r"°\s?[cс]|град\w*"),
    ("t", r"т\b|тонн\w*"), ("m", r"м\b|метр\w*"), ("sut", r"сут\w*"), ("h", r"ч\b|час\w*"),
]
_UNIT_AFTER_RX = re.compile(r"^\s{0,2}(?:" + "|".join(f"(?P<{k}>{v})" for k, v in _UNIT_FAMILIES) + ")", re.I)


def _mask_not_values(s: str) -> str:
    for rx in _NOT_VALUE_RXS:
        s = rx.sub(lambda m: " " * len(m.group(0)), s)
    return s


def _num_unit_pairs(s: str) -> tuple[set[str], set[tuple[str, str]]]:
    def _canon(t: str) -> str:
        t = re.sub(r"\s", "", t).replace(",", ".")
        return t.rstrip("0").rstrip(".") if "." in t else t      # 0,20 == 0.2, но 20 ≠ 2
    nums: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    for m in _NUM_TOK_RX.finditer(s or ""):
        c = _canon(m.group(0))
        nums.add(c)
        um = _UNIT_AFTER_RX.match(s[m.end(): m.end() + 14])
        if um:
            pairs.add((c, um.lastgroup or ""))
    return nums, pairs


def unsupported_numbers(text: str, context: str, *, strict_units: bool = True) -> list[str]:
    """Числа из «стало», которых нет в источниках (фрагменты, паспорт, замечание):
    сочинённые значения. Годы, номера пунктов/таблиц/источников, коды ФККО, даты,
    шифры и обозначения нормативов не считаются. Число С ЕДИНИЦЕЙ («200 м»,
    «70 дБА») подтверждается только той же парой «число + единица» в источниках
    (тестировщик №4: «СЗЗ 200 м» проходило, потому что «200» где-то встречалось)."""
    def _canon(t: str) -> str:
        t = re.sub(r"\s", "", t).replace(",", ".")
        return t.rstrip("0").rstrip(".") if "." in t else t
    ctx_nums, ctx_pairs = _num_unit_pairs(context or "")
    masked = _mask_not_values(text or "")
    out: list[str] = []
    for m in _NUM_TOK_RX.finditer(masked):
        raw = m.group(0)
        tok = re.sub(r"\s", "", raw).replace(",", ".")
        if len(tok.replace(".", "")) < 2 or re.fullmatch(r"(?:19|20)\d{2}", tok):
            continue
        c = _canon(raw)
        um = _UNIT_AFTER_RX.match(masked[m.end(): m.end() + 14])
        if um and strict_units:
            fam = um.lastgroup or ""
            if (c, fam) in ctx_pairs:
                continue
            label = f"{raw} {um.group(0).strip()}"
        else:
            if c in ctx_nums:
                continue
            label = raw
        if label not in out:
            out.append(label)
    return out


def location_from_remark(remark: str) -> str:
    """Адрес правки из текста замечания: «п.2.1.4», «табл. 3.33», «приложение 4.2»."""
    t = remark or ""
    parts = []
    for m in re.finditer(r"(?:п\.|пункт[аеу]?)\s*([\d.]+\d)", t, re.I):
        parts.append(f"п. {m.group(1)}")
    for m in re.finditer(r"(?:табл\.|таблиц[аеы])\s*([\d.]+\d)", t, re.I):
        parts.append(f"табл. {m.group(1)}")
    for m in re.finditer(r"приложени[еяи]\s*([\d.]+\d)", t, re.I):
        parts.append(f"приложение {m.group(1)}")
    return "; ".join(dict.fromkeys(parts))[:160]


def _norm_key(ident: str) -> str:
    """Числовое обозначение норматива из его названия: «ПП РФ № 913» → «913»."""
    toks = re.findall(r"\d[\d./\-]*\d|\d{3,}", ident or "")
    return max(toks, key=len) if toks else ""


def normatives_block(limit: int = 12, scope_text: str | None = None) -> str:
    """Справка ИИ об устаревших нормативах из data/normatives.yaml (replaced/
    cancelled): «X → заменён Y» — чтобы в «стало» не попадал ПП-913 как
    «актуальная редакция» (16.09.2026)."""
    try:
        from ..normatives.engine import _registry
        reg = _registry()
    except Exception:  # noqa: BLE001
        return ""
    lines = []
    for item in reg.values():
        st = str(item.get("status") or "")
        if st in ("replaced", "cancelled"):
            what = item.get("id", "")
            if scope_text is not None:
                # ТОЛЬКО ПО ТЕМЕ (тестировщик №4: блок целиком протекал в ответы —
                # СП 131 и СанПиН 1.2.3685-21 появились в 22 ответах как «обоснование»):
                # строка даётся, если устаревший документ назван в замечании/фрагментах
                key = _norm_key(str(what))
                if not key or not re.search(r"(?<![\d.])" + re.escape(key) + r"(?![\d])", scope_text):
                    continue
            rep = item.get("replaced_by") or ""
            lines.append(f"- {what}: {'заменён' if st == 'replaced' else 'отменён'}"
                         + (f" → {rep}" if rep else ""))
    return "\n".join(lines[:limit])


# ───────────── паспорт проекта по ПК ─────────────
_PASSPORT_KEYS = ("length_route", "workers", "duration", "area_plot")
_PASSPORT_PREFER = {"length_route": ("TKR", "AR", "PPO", "PZU", "POS"),
                    "area_plot": ("PPO", "PZU", "TKR", "AR", "POS"),
                    "workers": ("POS", "TKR", "PPO", "PZU"),
                    "duration": ("POS", "TKR", "PPO", "PZU")}


def passport(project: str) -> dict[str, dict]:
    """{'1': {'workers': {'value': '82', 'unit': 'чел.', 'file': …, 'alts': [...]}, …}, '2': …}
    — значения показателей по пусковым комплексам (по источнику варианта);
    длина — из ТКР (ПОС даёт длину этапа), сроки/численность — из ПОС; alts —
    расходящиеся значения из других разделов (в ответ выносится расхождение)."""
    from ..data import registry as R
    inds = (R.load_registry(project).get("indicators") or {})
    out: dict[str, dict] = {}
    for key in _PASSPORT_KEYS:
        rec = inds.get(key) or {}
        prefer = _PASSPORT_PREFER.get(key, ("POS", "TKR", "PPO", "PZU"))
        cands: dict[str, list[tuple[tuple, dict]]] = {}
        for v in rec.get("variants") or []:
            for s in v.get("sources") or []:
                pk = pk_of(volume_token(s.get("file", "")))
                if not pk:
                    continue
                pri = prefer.index(s.get("section")) if s.get("section") in prefer else 9
                cands.setdefault(pk, []).append(((pri, -int(v.get("count", 0))),
                                                 {"value": v.get("value"), "unit": v.get("unit") or rec.get("unit", ""),
                                                  "file": s.get("file", ""), "loc": s.get("loc", ""),
                                                  "section": s.get("section", "")}))
        for pk, lst in cands.items():
            lst.sort(key=lambda x: x[0])
            best = lst[0][1]
            alts = []
            for _, d in lst[1:]:
                if d["value"] != best["value"] and d["section"] != best["section"] and \
                        all(d["value"] != a["value"] for a in alts):
                    alts.append(d)
            out.setdefault(pk, {})[key] = dict(best, alts=alts[:2])
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
        parts = []
        for k, d in vals.items():
            s = f"{labels.get(k, k)}: {d['value']} {d['unit']} [{d['file']}, {d['loc']}]"
            if d.get("alts"):
                s += " (РАСХОЖДЕНИЕ: " + "; ".join(f"{a['value']} {a['unit']} по {a['file']}" for a in d["alts"]) + \
                     " — укажи расхождение в ответе)"
            parts.append(s)
        lines.append(f"- {head}: " + "; ".join(parts))
    return "\n".join(lines)
