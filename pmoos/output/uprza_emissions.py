# -*- coding: utf-8 -*-
"""Выбросы ЗВ из таблиц тома ООС (v0.55, 15.09.2026).

Проверка на ОПОЧКЕ: выгрузка для УПРЗА отдавала коды веществ с ПУСТЫМИ г/с и
т/год, потому что collect_emissions искал числа только в тексте ответов ИИ.
В самом томе ООС есть таблицы «Перечень загрязняющих веществ» (код ·
наименование · критерий · класс · г/с · т/год) — после декодирования шрифта
их текст читается построчно:

    0301 Азота диоксид (Двуокись азота;
    пероксид азота)
    ПДК м/р 0,20000
    3
    0,3677651
    1,880935

parse_emission_rows() собирает такие строки из плоского текста (страница /
чанк), collect_from_index() — по всем чанкам томов целевого раздела.
"""
from __future__ import annotations

import re

_CODE_RX = re.compile(r"^\s*(\d{4})\s+([А-ЯЁA-Z][^\n]*)$")
_NUM_RX = re.compile(r"^\s*(\d+[.,]\d+(?:[eE][-+]?\d+)?|\d+[.,]\d+e-\d+)\s*$")
_CRIT_RX = re.compile(r"(ПДК\s*(?:м/р|с/с|мр|сс)|ОБУВ)\s*([\d.,]+)?", re.I)
_CLASS_RX = re.compile(r"^\s*([1-4])\s*$")
_CAPTION_RX = re.compile(r"(период[а-я]*\s+(?:строительств|эксплуатац|реконструкц)[а-я]*|"
                         r"строительн[а-я]+\s+период|эксплуатац[а-я]+\s+период)", re.I)


def _num(s: str) -> float | None:
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def parse_emission_rows(text: str) -> list[dict]:
    """Строки таблицы выбросов из плоского текста страницы/чанка."""
    lines = [l.rstrip() for l in (text or "").splitlines()]
    rows: list[dict] = []
    period = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        mcap = _CAPTION_RX.search(line)
        if mcap:
            period = mcap.group(0)
        m = _CODE_RX.match(line)
        if not m:
            i += 1
            continue
        code, name = m.group(1), m.group(2).strip()
        j = i + 1
        # продолжение названия (строка без чисел/критерия, до 2 строк)
        while j < len(lines) and j <= i + 2 and lines[j].strip() and not _CRIT_RX.search(lines[j]) \
                and not _NUM_RX.match(lines[j]) and not _CODE_RX.match(lines[j]) and not _CLASS_RX.match(lines[j]):
            piece = lines[j].strip()
            name = (name[:-1] + piece) if name.endswith("-") else (name + " " + piece)
            j += 1
        # критерий может стоять в той же строке, что и название («… ПДК м/р 1,00»)
        crit, crit_val, klass = "", None, ""
        mc0 = _CRIT_RX.search(name)
        if mc0:
            crit = mc0.group(1).upper().replace("МР", "м/р").replace("СС", "с/с")
            crit_val = _num(mc0.group(2)) if mc0.group(2) else None
            name = name[:mc0.start()].strip()
        nums: list[float] = []
        k = j
        while k < len(lines) and k <= j + 7 and not _CODE_RX.match(lines[k]):
            s = lines[k].strip()
            mc = _CRIT_RX.search(s)
            if mc and not crit:
                crit = mc.group(1).upper().replace("МР", "м/р").replace("СС", "с/с")
                crit_val = _num(mc.group(2)) if mc.group(2) else None
                k += 1
                continue
            if crit and crit_val is None and re.fullmatch(r"\d+", s):
                crit_val = float(s)       # «ОБУВ» + «50» на следующей строке
                k += 1
                continue
            if _CLASS_RX.match(s) and not klass and not nums:
                klass = s
                k += 1
                continue
            if _NUM_RX.match(s):
                v = _num(s)
                if v is not None:
                    if crit and crit_val is None:
                        crit_val = v          # значение критерия (ОБУВ 50) — не выброс
                    else:
                        nums.append(v)
                k += 1
                if len(nums) >= 2:
                    break
                continue
            if not s:
                k += 1
                continue
            break
        if len(nums) >= 2:
            rows.append({"code": code, "name": re.sub(r"\s+", " ", name).strip(" ;"),
                         "criterion": crit, "criterion_value": crit_val, "class": klass,
                         "g_s": nums[0], "t_year": nums[1], "period": period})
            i = k
        else:
            i += 1
    return rows


def merge_rows(rows: list[dict]) -> list[dict]:
    """Одно вещество — одна строка: максимум г/с и сумма т/год по периодам
    (несколько таблиц одного тома дублируют вещество: строительство,
    эксплуатация, суммарно). Берём МАКСИМАЛЬНЫЕ значения — для УПРЗА это
    консервативно и не занижает."""
    by: dict[str, dict] = {}
    for r in rows:
        cur = by.get(r["code"])
        if cur is None:
            by[r["code"]] = dict(r, sources=1)
        else:
            cur["g_s"] = max(cur["g_s"], r["g_s"])
            cur["t_year"] = max(cur["t_year"], r["t_year"])
            cur["sources"] += 1
            if not cur.get("class") and r.get("class"):
                cur["class"] = r["class"]
            if len(r["name"]) > len(cur["name"]):
                cur["name"] = r["name"]
    return sorted(by.values(), key=lambda r: r["code"])


def collect_from_index(project: str, cfg=None, target: str = "OOS",
                       files: list[str] | None = None) -> tuple[list[dict], dict]:
    """Все строки выбросов из чанков томов целевого раздела → сводка по
    веществам + по файлам (какие тома дали данные)."""
    from ..config import load_config
    from ..index.vectorstore import VectorStore, collection_name
    from ..pipeline.volumes import oos_volumes
    cfg = cfg or load_config()
    files = files or list(oos_volumes(project, target).values())
    store = VectorStore(cfg, dim=1024)
    rows: list[dict] = []
    per_file: dict[str, int] = {}
    try:
        client = store.client()
        coll = collection_name(project)
        offset = None
        while True:
            pts, offset = client.scroll(collection_name=coll, with_payload=True,
                                        with_vectors=False, limit=512, offset=offset)
            for p in pts:
                pl = p.payload or {}
                if files and pl.get("file") not in files:
                    continue
                got = parse_emission_rows(pl.get("text", "") or "")
                for r in got:
                    r["file"] = pl.get("file", "")
                    r["loc"] = pl.get("loc", "")
                rows.extend(got)
                if got:
                    per_file[pl.get("file", "")] = per_file.get(pl.get("file", ""), 0) + len(got)
            if offset is None:
                break
    finally:
        store.close()
    return merge_rows(rows), {"per_file": per_file, "raw": len(rows)}
