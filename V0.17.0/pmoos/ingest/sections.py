"""Состав проектной документации по Постановлению Правительства РФ №87
«О составе разделов проектной документации и требованиях к их содержанию».

Здесь учтены замечания пользователя:
  * «разделов больше, чем показывает приложение» — даны полные составы;
  * «меняется линейный/площадной — должны меняться автоматом разделы, которые
     нужны в составе ПД» — два набора (площадной/линейный) и required_sections();
  * ТКР — это Раздел 3 ИМЕННО для линейных объектов
     («Технологические и конструктивные решения линейного объекта»),
     поэтому ТКР появляется в составе только при object_type='линейный'.

Каждый раздел: code, num (номер по ПП-87), name, short (аббревиатура),
keywords (для авто-распознавания по имени файла), is_oos (целевой раздел),
is_source (является источником данных для ответов по ООС).
"""
from __future__ import annotations

import re

# --- состав для ПЛОЩАДНЫХ объектов (объекты капитального строительства) -----
SECTIONS_AREAL: list[dict] = [
    {"code": "PZ",   "num": "1",    "short": "ПЗ",   "name": "Пояснительная записка"},
    {"code": "PZU",  "num": "2",    "short": "СПОЗУ","name": "Схема планировочной организации земельного участка"},
    {"code": "AR",   "num": "3",    "short": "АР",   "name": "Архитектурные решения"},
    {"code": "KR",   "num": "4",    "short": "КР",   "name": "Конструктивные и объёмно-планировочные решения", "is_source": True},
    {"code": "IOS",  "num": "5",    "short": "ИОС",  "name": "Сведения об инженерном оборудовании, о сетях инженерно-технического обеспечения"},
    {"code": "IOS_EOM","num": "5.1","short": "ЭОМ",  "name": "Система электроснабжения"},
    {"code": "IOS_VK", "num": "5.2","short": "ВК",   "name": "Система водоснабжения"},
    {"code": "IOS_VO", "num": "5.3","short": "ВО",   "name": "Система водоотведения"},
    {"code": "IOS_OV", "num": "5.4","short": "ОВиК", "name": "Отопление, вентиляция и кондиционирование"},
    {"code": "IOS_SS", "num": "5.5","short": "СС",   "name": "Сети связи"},
    {"code": "IOS_GS", "num": "5.6","short": "ГС",   "name": "Система газоснабжения"},
    {"code": "IOS_TH", "num": "5.7","short": "ТХ",   "name": "Технологические решения", "is_source": True},
    {"code": "POS",  "num": "6",    "short": "ПОС",  "name": "Проект организации строительства", "is_source": True},
    {"code": "POD",  "num": "7",    "short": "ПОД",  "name": "Проект организации работ по сносу и демонтажу"},
    {"code": "OOS",  "num": "8",    "short": "ПМООС","name": "Перечень мероприятий по охране окружающей среды", "is_oos": True, "is_source": True},
    {"code": "PB",   "num": "9",    "short": "ПБ",   "name": "Мероприятия по обеспечению пожарной безопасности"},
    {"code": "ODI",  "num": "10",   "short": "ОДИ",  "name": "Мероприятия по обеспечению доступа инвалидов"},
    {"code": "EE",   "num": "10.1", "short": "ЭЭ",   "name": "Мероприятия по обеспечению соблюдения требований энергетической эффективности"},
    {"code": "SM",   "num": "11",   "short": "СМ",   "name": "Смета на строительство"},
    {"code": "GOCHS","num": "11.1", "short": "ГОЧС", "name": "Перечень мероприятий по гражданской обороне, мероприятий по предупреждению ЧС"},
    {"code": "OTHER","num": "12",   "short": "ИД",   "name": "Иная документация в случаях, предусмотренных законами"},
]

# --- состав для ЛИНЕЙНЫХ объектов (трубопроводы, дороги, ЛЭП и т.п.) ---------
SECTIONS_LINEAR: list[dict] = [
    {"code": "PZ",   "num": "1",  "short": "ПЗ",  "name": "Пояснительная записка"},
    {"code": "PPO",  "num": "2",  "short": "ППО", "name": "Проект полосы отвода"},
    {"code": "TKR",  "num": "3",  "short": "ТКР", "name": "Технологические и конструктивные решения линейного объекта. Искусственные сооружения", "is_source": True},
    {"code": "ZSS",  "num": "4",  "short": "ЗСС", "name": "Здания, строения и сооружения, входящие в инфраструктуру линейного объекта", "is_source": True},
    {"code": "POS",  "num": "5",  "short": "ПОС", "name": "Проект организации строительства", "is_source": True},
    {"code": "POD",  "num": "6",  "short": "ПОД", "name": "Проект организации работ по сносу и демонтажу"},
    {"code": "OOS",  "num": "7",  "short": "ПМООС","name": "Мероприятия по охране окружающей среды", "is_oos": True, "is_source": True},
    {"code": "PB",   "num": "8",  "short": "ПБ",  "name": "Мероприятия по обеспечению пожарной безопасности"},
    {"code": "SM",   "num": "9",  "short": "СМ",  "name": "Смета на строительство"},
    {"code": "GOCHS","num": "9.1","short": "ГОЧС","name": "Перечень мероприятий по гражданской обороне, предупреждению ЧС"},
    {"code": "OTHER","num": "10", "short": "ИД",  "name": "Иная документация"},
]

# --- инженерные изыскания (не разделы ПД, но ИСТОЧНИКИ данных для ООС) -------
SURVEYS: list[dict] = [
    {"code": "IEI",  "short": "ИЭИ",  "name": "Инженерно-экологические изыскания", "is_source": True},
    {"code": "IGMI", "short": "ИГМИ", "name": "Инженерно-гидрометеорологические изыскания", "is_source": True},
    {"code": "IGI",  "short": "ИГИ",  "name": "Инженерно-геологические изыскания", "is_source": True},
    {"code": "IGDI", "short": "ИГДИ", "name": "Инженерно-геодезические изыскания"},
    {"code": "IGEI", "short": "ИГЭИ", "name": "Инженерно-геотехнические изыскания"},
]

# ключевые слова распознавания: short-токены (целое слово) + длинные подстроки
KEYWORDS: dict[str, dict] = {
    "OOS":   {"tok": ["оос", "пмоос", "ос"], "sub": ["охран окружающ", "охрана окружающ", "мероприятий по охране", "экологическ обоснов"]},
    "TKR":   {"tok": ["ткр"], "sub": ["технологическ и конструктивн", "конструктивн решения линейн", "искусственные сооружен"]},
    "POS":   {"tok": ["пос"], "sub": ["организац строительств", "организации строительства"]},
    "POD":   {"tok": ["под"], "sub": ["организац работ по сносу", "снос", "демонтаж"]},
    "IEI":   {"tok": ["иэи"], "sub": ["инженерно-экологическ", "инженерно экологическ", "экологическ изыскан"]},
    "IGMI":  {"tok": ["игми"], "sub": ["гидрометеоролог"]},
    "IGI":   {"tok": ["иги"], "sub": ["инженерно-геологическ", "геологическ изыскан"]},
    "IGDI":  {"tok": ["игди"], "sub": ["геодезическ"]},
    "PZ":    {"tok": ["пз"], "sub": ["пояснительн записк"]},
    "PZU":   {"tok": ["пзу", "спозу"], "sub": ["планировочн организац земельн"]},
    "AR":    {"tok": ["ар"], "sub": ["архитектурн решен"]},
    "KR":    {"tok": ["кр"], "sub": ["конструктивн", "объёмно-планировочн", "объемно-планировочн"]},
    "IOS":   {"tok": ["иос"], "sub": ["инженерн оборудован", "сети инженерно-техническ"]},
    "IOS_EOM":{"tok": ["эом"], "sub": ["электроснабжен"]},
    "IOS_VK":{"tok": ["вк"], "sub": ["водоснабжен"]},
    "IOS_VO":{"tok": ["во"], "sub": ["водоотведен"]},
    "IOS_OV":{"tok": ["ов", "овик"], "sub": ["отоплен", "вентиляц", "кондиционирован"]},
    "IOS_SS":{"tok": ["сс"], "sub": ["сети связи", "слаботочн"]},
    "IOS_GS":{"tok": ["гс"], "sub": ["газоснабжен"]},
    "IOS_TH":{"tok": ["тх"], "sub": ["технологическ решен"]},
    "PPO":   {"tok": ["ппо"], "sub": ["полос отвод"]},
    "ZSS":   {"tok": ["зсс"], "sub": ["здания строения и сооружен"]},
    "PB":    {"tok": ["пб"], "sub": ["пожарн безопасност"]},
    "ODI":   {"tok": ["оди"], "sub": ["доступ инвалид", "маломобильн"]},
    "EE":    {"tok": ["ээ"], "sub": ["энергетическ эффективн", "энергоэффективн"]},
    "SM":    {"tok": ["см", "сметы", "смета"], "sub": ["сметн", "сметная документац"]},
    "GOCHS": {"tok": ["гочс", "итм"], "sub": ["гражданск оборон", "чрезвычайн ситуац", "предупреждению чс"]},
}

VERSION_HINTS = [
    (re.compile(r"\bкорр\w*", re.I), "корректировка"),
    (re.compile(r"\bиспр\w*", re.I), "исправленная"),
    (re.compile(r"\bизм\w*", re.I), "с изменениями"),
    (re.compile(r"\bфинал\w*|\bfinal\b", re.I), "финальная"),
    (re.compile(r"\bред\w*\.?\s*\d+|\brev\.?\s*\d+", re.I), "редакция"),
    (re.compile(r"\bv\s*\.?\s*(\d+)", re.I), "версия"),
    (re.compile(r"\b(\d{2})[._-](\d{2})[._-](\d{2,4})\b"), "дата"),
]


def all_sections() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for s in SECTIONS_AREAL + SECTIONS_LINEAR + SURVEYS:
        out.setdefault(s["code"], s)
    return out


def required_sections(object_type: str) -> list[dict]:
    """Обязательный состав ПД в зависимости от типа объекта (ПП-87)."""
    base = SECTIONS_LINEAR if str(object_type).startswith("лин") else SECTIONS_AREAL
    return [dict(s) for s in base]


def section_name(code: str) -> str:
    s = all_sections().get(code)
    return f"{s['short']} — {s['name']}" if s else code


def section_num(code: str) -> str:
    """Номер раздела по ПП-87 (например '8' или '5.4'); '' если неизвестно."""
    s = all_sections().get(code)
    return s.get("num", "") if s else ""


def section_short(code: str) -> str:
    """Краткая аббревиатура раздела (ПМООС/ПОС/ТКР...); сам код, если неизвестно."""
    s = all_sections().get(code)
    return s.get("short", code) if s else code


def source_section_codes(object_type: str) -> list[str]:
    """Коды разделов-источников для ответов по ООС (ТКР/ПОС/ИЭИ/КР/ТХ и сам ООС)."""
    codes = [s["code"] for s in required_sections(object_type) if s.get("is_source")]
    codes += [s["code"] for s in SURVEYS if s.get("is_source")]
    return sorted(set(codes))


def detect_version_hint(filename: str) -> str | None:
    # заменяем разделители на пробелы, чтобы _корр_ и -v2 распознавались
    name = re.sub(r"[._\-]+", " ", filename or "")
    hits = [label for rx, label in VERSION_HINTS if rx.search(name)]
    return ", ".join(dict.fromkeys(hits)) or None


def classify_filename(filename: str, object_type: str = "площадной", top: int = 3) -> list[dict]:
    """Эвристическое определение раздела по имени файла.
    Возвращает ранжированный список [{code, score, name}]."""
    name = (filename or "").lower()
    # нормализуем разделители
    norm = re.sub(r"[._\-]+", " ", name)
    tokens = set(re.findall(r"[а-яёa-z0-9]+", norm))
    valid = {s["code"] for s in required_sections(object_type)} | {s["code"] for s in SURVEYS}

    scores: dict[str, float] = {}
    for code, kw in KEYWORDS.items():
        if code not in valid:
            continue
        sc = 0.0
        for t in kw.get("tok", []):
            if t in tokens:
                # более длинные/специфичные токены ценнее
                sc += 5.0 + 0.5 * len(t)
        for sub in kw.get("sub", []):
            parts = sub.split()
            if parts and all(p in norm for p in parts):
                sc += 3.0
        if sc:
            scores[code] = sc
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top]
    names = all_sections()
    return [{"code": c, "score": round(s, 1),
             "name": section_name(c), "short": names.get(c, {}).get("short", c)}
            for c, s in ranked]


def guess_object_type(filenames: list[str]) -> str:
    """Грубая авто-оценка типа объекта по набору имён файлов."""
    blob = " ".join(filenames).lower()
    linear_signals = ["ппо", "полос отвод", "линейн", "трасс", "км ", "трубопровод",
                      "газопровод", "нефтепровод", "автомобильн дорог", "лэп", "ткр"]
    score = sum(1 for s in linear_signals if s in blob)
    return "линейный" if score >= 2 else "площадной"
