"""Дедупликация документов по содержимому.

Замечание ревью: ТКР_v1.pdf / ТКР_корр.pdf / ТКР_финал.pdf проиндексируются как
разные документы и дадут «смешивание версий» и ложные ответы.
Здесь — отпечаток документа sha256(нормализованный текст). Одинаковые файлы
(побайтно или по содержанию) не индексируются повторно; похожие группируются
как версии одного документа (см. versioning/versions.py).
"""
from __future__ import annotations

import hashlib
import re


def doc_fingerprint(pages: list[dict]) -> str:
    """sha256 нормализованного полного текста документа."""
    full = "\n".join(p.get("text", "") for p in pages)
    norm = re.sub(r"\s+", " ", full).strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def shingles(text: str, k: int = 5) -> set[str]:
    words = re.findall(r"\w+", (text or "").lower())
    return {" ".join(words[i:i + k]) for i in range(max(0, len(words) - k + 1))}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def similarity(pages_a: list[dict], pages_b: list[dict], *, sample_chars: int = 20000) -> float:
    """Грубая оценка похожести двух документов (для группировки версий)."""
    ta = "\n".join(p.get("text", "") for p in pages_a)[:sample_chars]
    tb = "\n".join(p.get("text", "") for p in pages_b)[:sample_chars]
    return jaccard(shingles(ta), shingles(tb))


# ───────────────────── контентные подписи (SimHash) ─────────────────────
# Пункт 4 ревью: версии нужно различать по СОДЕРЖИМОМУ, а не только по имени.
# SimHash даёт компактную (64-битную) подпись документа; близкое расстояние
# Хэмминга => почти одинаковый контент (тот же документ под другим именем),
# далёкое => разный контент (похожее имя, но другой документ). Файлы при этом
# НЕ хранятся (требование #9) — подпись считается при индексации и сохраняется.
def simhash(text: str, *, bits: int = 64, k: int = 4) -> int:
    words = re.findall(r"\w+", (text or "").lower())
    if not words:
        return 0
    feats: dict[str, int] = {}
    for i in range(max(1, len(words) - k + 1)):
        sh = " ".join(words[i:i + k])
        feats[sh] = feats.get(sh, 0) + 1
    if not feats:
        return 0
    v = [0] * bits
    mask = (1 << bits) - 1
    for feat, w in feats.items():
        h = int.from_bytes(hashlib.blake2b(feat.encode("utf-8"), digest_size=bits // 8).digest(), "big") & mask
        for b in range(bits):
            v[b] += w if (h >> b) & 1 else -w
    out = 0
    for b in range(bits):
        if v[b] > 0:
            out |= (1 << b)
    return out


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def simhash_similarity(a: int, b: int, *, bits: int = 64) -> float:
    """1.0 — идентичны, 0.0 — максимально различны (по расстоянию Хэмминга)."""
    if a == 0 and b == 0:
        return 0.0
    return 1.0 - hamming(a, b) / bits


def content_signature(pages: list[dict], *, sample_chars: int = 60000) -> dict:
    """Подпись документа: точный sha256 + near-dup SimHash + MinHash + объём.

    MinHash надёжнее SimHash для коротких/повторяющихся текстов (прямая оценка
    Жаккара), поэтому сравнение версий использует именно его; SimHash оставлен
    как компактный запасной сигнал.
    """
    full = "\n".join(p.get("text", "") for p in pages)
    sample = full[:sample_chars]
    return {
        "sha256": doc_fingerprint(pages),
        "simhash": simhash(sample),
        "minhash": minhash(sample),
        "n_chars": len(full),
    }


# MinHash: оценка коэффициента Жаккара по совпадению мин-хэшей (детерминированно).
import random as _random  # noqa: E402

_MH_N = 64
_MH_PRIME = (1 << 61) - 1
_mh_rng = _random.Random(20240601)
_MH_A = [_mh_rng.randrange(1, _MH_PRIME) for _ in range(_MH_N)]
_MH_B = [_mh_rng.randrange(0, _MH_PRIME) for _ in range(_MH_N)]


def minhash(text: str, *, k: int = 5, num_perm: int = _MH_N) -> list[int]:
    sh = shingles(text, k)
    if not sh:
        return [0] * num_perm
    base = [int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big")
            for s in sh]
    out: list[int] = []
    for i in range(num_perm):
        a, b = _MH_A[i], _MH_B[i]
        out.append(min((a * h + b) % _MH_PRIME for h in base))
    return out


def minhash_similarity(a: list[int], b: list[int]) -> float:
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    return sum(1 for i in range(n) if a[i] == b[i]) / n
