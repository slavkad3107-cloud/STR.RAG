"""Гибридный поиск: плотные векторы (Qdrant) + BM25 + RRF, затем реранк.

Зачем гибрид: канцелярский язык замечаний плохо ловится только семантикой,
а точные термины/номера ГОСТ/обозначения веществ лучше находит лексический
BM25. Объединяем результаты через Reciprocal Rank Fusion и финально уточняем
кросс-энкодером.

BM25 строится по чанкам коллекции проекта. Чтобы не гонять весь индекс на
каждый запрос, корпус кэшируется в памяти на время сессии пайплайна.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..config import Config
from ..index.embeddings import Embedder
from ..index.vectorstore import VectorStore, collection_name
from ..normatives.engine import find_references
from .query_expansion import expand_query, expand_query_batch
from .reranker import Reranker

_WORD = re.compile(r"[\w\-]+", re.UNICODE)
_WS = re.compile(r"\s+")


def _norm_code(ref: str) -> str:
    """Единый токен нормативного обозначения: нижний регистр, без пробелов и №.

    Срезаем и завершающие '-'/'/' — регэксп нормативов жадно их захватывает
    («СП 47.13330-» на границе чанка), и без среза токен документа не совпал бы
    с токеном запроса («СП 47.13330»)."""
    return _WS.sub("", (ref or "").lower()).replace("№", "").rstrip("-/")


def _tok(text: str) -> list[str]:
    """Токенизация для BM25.

    Обычный паттерн [\\w\\-]+ рвёт нормативные коды по '/', '.' и пробелам
    («СанПиН 2.2.1/2.1.1.1200-03» → 'санпин','2','2','1',…), и точное совпадение
    кода между замечанием и документом теряется. Поэтому ДОПОЛНИТЕЛЬНО к обычным
    словам добавляем целые нормативные обозначения одним токеном (через тот же
    реестр-регэксп, что и движок нормативов) — так BM25 ловит точные короды.
    """
    text = text or ""
    # sys.intern: одинаковые словоформы («выбросы» в тысячах чанков) делят ОДИН
    # str-объект — на 20-50k чанков это сотни МБ RAM (t.lower() иначе создаёт
    # новый объект на каждое вхождение). Значения строк не меняются → BM25
    # бит-в-бит идентичен.
    import sys as _sys
    toks = [_sys.intern(t.lower()) for t in _WORD.findall(text)]
    # find_references — чистый regex; цифр нет → нормативного кода точно нет,
    # пропускаем дорогой проход (заметно дешевле на сборке BM25-корпуса).
    if any(c.isdigit() for c in text):
        try:
            for ref in find_references(text):
                code = _norm_code(ref)
                if code:
                    toks.append(_sys.intern(code))
        except Exception:  # noqa: BLE001
            pass
    return toks


def expand_hits(hits: list[dict], index: dict, *, neighbors: int = 1,
                merge_tables: bool = True) -> list[dict]:
    """Расширить найденные чанки контекстом (пункт 3B).

    Для каждого попадания добавляет соседние чанки того же файла (±neighbors по
    chunk_index); если чанк табличный и merge_tables=True — захватывает все ПОДРЯД
    идущие табличные чанки (восстанавливает таблицу целиком, напр. таблицу выбросов).
    Текст попадания заменяется на склейку в порядке chunk_index. Дедупликация по
    id, чтобы один и тот же блок не уходил в ИИ много раз.

    index: dict[(file, chunk_index)] -> {"id", "text", "is_table"}.
    """
    if not index:
        return hits
    used: set[str] = set()
    out: list[dict] = []
    for h in hits:
        pl = h.get("payload", {}) or {}
        f = pl.get("file")
        idx = pl.get("chunk_index")
        if f is None or idx is None or (f, idx) not in index:
            out.append(h)
            continue
        want = set(range(idx - neighbors, idx + neighbors + 1))
        if merge_tables and pl.get("is_table"):
            lo = idx
            while (f, lo - 1) in index and index[(f, lo - 1)].get("is_table"):
                lo -= 1
            hi = idx
            while (f, hi + 1) in index and index[(f, hi + 1)].get("is_table"):
                hi += 1
            want |= set(range(lo, hi + 1))
        parts, merged = [], []
        for i in sorted(want):
            cell = index.get((f, i))
            if not cell or cell["id"] in used:
                continue
            if cell.get("text"):
                parts.append(cell["text"])
                merged.append(cell["id"])
        if not parts:
            out.append(h)
            continue
        used.update(merged)
        new = dict(h)
        new["text"] = "\n…\n".join(parts).strip()
        new["expanded_from"] = merged
        out.append(new)
    return out


@dataclass
class _Corpus:
    ids: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    payloads: list[dict] = field(default_factory=list)
    bm25: Any = None
    by_index: dict = field(default_factory=dict)  # (file, chunk_index) -> {id,text,is_table}
    tokens: Any = None  # предвычисленные токены для BM25 (персистятся на диск)


class HybridRetriever:
    def __init__(self, cfg: Config, *, embedder: Embedder | None = None,
                 store: VectorStore | None = None, reranker: Reranker | None = None):
        self.cfg = cfg
        self.embedder = embedder or Embedder(cfg)
        # dim — ЛЕНИВО (callable): self.embedder.dim грузит модель ~2.3 ГБ, а в
        # поисковом пути dim не нужен (коллекция уже существует). Модель загрузится
        # при первом реальном embed, а не при создании ретривера.
        self.store = store or VectorStore(cfg, dim=lambda: self.embedder.dim)
        self.reranker = reranker or Reranker(cfg)
        self._corpus_cache: dict[str, _Corpus] = {}

    # ---- BM25 corpus -----------------------------------------------------
    def close(self) -> None:
        """Освободить embedded-Qdrant (однопроцессный) сразу после поиска."""
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass

    def _corpus_file(self, project: str):
        from ..paths import project_paths
        return project_paths(project)["root"] / "bm25_corpus.pkl"

    def _scroll_corpus(self, project: str) -> _Corpus:
        """Полное чтение коллекции из Qdrant (дорого — только при изменении индекса)."""
        corp = _Corpus()
        try:
            client = self.store.client()
            name = collection_name(project)
            offset = None
            while True:
                points, offset = client.scroll(
                    collection_name=name, with_payload=True, with_vectors=False,
                    limit=512, offset=offset,
                )
                import sys as _sys
                for p in points:
                    pl = p.payload or {}
                    # intern повторяющихся значений: 'file'/'section' одинаковы у
                    # всех чанков одного файла, но scroll даёт отдельные str-копии
                    for _k in ("file", "section"):
                        _v = pl.get(_k)
                        if isinstance(_v, str):
                            pl[_k] = _sys.intern(_v)
                    corp.ids.append(str(p.id))
                    corp.texts.append(pl.get("text", ""))
                    corp.payloads.append(pl)
                if offset is None:
                    break
        except Exception:  # noqa: BLE001
            pass
        corp.tokens = [_tok(t) for t in corp.texts]
        return corp

    @staticmethod
    def _build_by_index(corp: _Corpus) -> None:
        bx: dict = {}
        for cid, txt, pl in zip(corp.ids, corp.texts, corp.payloads):
            f = (pl or {}).get("file")
            ci = (pl or {}).get("chunk_index")
            if f is not None and ci is not None:
                bx[(f, ci)] = {"id": cid, "text": txt, "is_table": bool(pl.get("is_table"))}
        corp.by_index = bx

    def _build_bm25(self, corp: _Corpus) -> None:
        if corp.tokens is None:
            corp.tokens = [_tok(t) for t in corp.texts]
        if corp.texts and self.cfg.get("retrieval.use_bm25", True):
            try:
                from rank_bm25 import BM25Okapi
                corp.bm25 = BM25Okapi(corp.tokens)
            except Exception:  # noqa: BLE001
                corp.bm25 = None

    def _load_corpus(self, project: str) -> _Corpus:
        """BM25-корпус с ПЕРСИСТЕНТНЫМ кэшем (оптимизация М4).

        Раньше при каждом запуске пайплайна корпус собирался scroll'ом ВСЕЙ
        коллекции Qdrant + полной токенизацией. Теперь ids/texts/payloads/tokens
        кэшируются на диск (root/bm25_corpus.pkl); инвалидация — по числу точек
        в коллекции (store.count): индекс не менялся → читаем с диска без scroll.
        """
        if project in self._corpus_cache:
            return self._corpus_cache[project]
        try:
            count = int(self.store.count(project))
        except Exception:  # noqa: BLE001
            count = -1

        corp: _Corpus | None = None
        fp = self._corpus_file(project)
        if count > 0 and fp.exists():
            try:
                import pickle
                import sys as _sys
                # потоковый pickle.load: read_bytes() держал бы весь блоб
                # (~60-180 МБ на 40-50k чанков) поверх построенных объектов
                with fp.open("rb") as f:
                    data = pickle.load(f)
                if (data.get("count") == count and isinstance(data.get("ids"), list)
                        and len(data["ids"]) == count):
                    toks = data.get("tokens")
                    if toks:  # ре-интернируем токены из старого pkl (см. _tok)
                        toks = [[_sys.intern(t) for t in row] for row in toks]
                    corp = _Corpus(ids=data["ids"], texts=data["texts"],
                                   payloads=data["payloads"], tokens=toks)
            except Exception:  # noqa: BLE001
                corp = None

        if corp is None:  # промах кэша / индекс изменился → читаем Qdrant и сохраняем
            corp = self._scroll_corpus(project)
            if corp.ids:
                try:
                    import pickle
                    fp.parent.mkdir(parents=True, exist_ok=True)
                    tmp = fp.with_suffix(".pkl.tmp")
                    with tmp.open("wb") as f:  # потоково, без промежуточного блоба
                        pickle.dump({
                            "count": len(corp.ids), "ids": corp.ids,
                            "texts": corp.texts, "payloads": corp.payloads,
                            "tokens": corp.tokens,
                        }, f)
                    tmp.replace(fp)
                except Exception:  # noqa: BLE001
                    pass

        self._build_by_index(corp)
        self._build_bm25(corp)
        # токены после сборки BM25 больше нигде не нужны (запрос токенизируется
        # заново) — освобождаем десятки-сотни МБ на большой базе. pkl уже записан.
        corp.tokens = None
        self._corpus_cache[project] = corp
        return corp

    # ---- single components ----------------------------------------------
    def _dense(self, project: str, query: str, *, candidates: int,
               sections, exclude_sections) -> list[dict]:
        qv = self.embedder.embed_queries([query])[0]
        return self.store.search(project, qv, top=candidates,
                                 sections=sections, exclude_sections=exclude_sections)

    def _bm25(self, project: str, query: str, *, candidates: int,
              sections, exclude_sections) -> list[dict]:
        corp = self._load_corpus(project)
        if not corp.bm25:
            return []
        scores = corp.bm25.get_scores(_tok(query))
        # argsort C-уровня вместо Python-сортировки всего корпуса на каждый запрос;
        # kind="stable" + минус сохраняет прежний порядок при равных баллах.
        import numpy as _np
        order = _np.argsort(-_np.asarray(scores), kind="stable")
        out: list[dict] = []
        sset = set(sections) if sections else None
        xset = set(exclude_sections) if exclude_sections else None
        for i in order:
            if float(scores[i]) <= 0.0:
                break  # порядок убывающий: дальше только нерелевантные (нулевые)
            pl = corp.payloads[i]
            sec = pl.get("section")
            if sset and sec not in sset:
                continue
            if xset and sec in xset:
                continue
            out.append({"id": corp.ids[i], "score": float(scores[i]),
                        "text": corp.texts[i], "payload": pl})
            if len(out) >= candidates:
                break
        return out

    @staticmethod
    def _rrf(result_lists: list[list[dict]], *, k: int = 60,
             weights: list[float] | None = None,
             groups: list[str] | None = None) -> list[dict]:
        """Reciprocal Rank Fusion по id чанка с ПОСПИСОЧНЫМИ весами.

        weights (параллельно result_lists) позволяет не дать одному BM25-списку
        утонуть среди нескольких dense-списков расширения запроса.

        groups (опционально, параллельно result_lists): списки с меткой "dense"
        нормируются ПОДОКУМЕНТНО — вклад документа делится на число dense-списков,
        где он встретился (средний reciprocal-rank), а не на общее число списков.
        Иначе (деление веса на N списков) одиночная чисто-семантическая находка
        получала бы 1/N веса и тонула ниже ХУДШЕГО элемента полного BM25-списка.
        """
        agg: dict[str, dict] = {}
        for li, lst in enumerate(result_lists):
            w = 1.0 if weights is None else float(weights[li])
            grp = groups[li] if groups else None
            for rank, item in enumerate(lst):
                cid = item["id"]
                node = agg.setdefault(cid, {"item": item, "rrf": 0.0,
                                            "d_sum": 0.0, "d_cnt": 0})
                rr = w * (1.0 / (k + rank + 1))
                if grp == "dense":
                    node["d_sum"] += rr
                    node["d_cnt"] += 1
                else:
                    node["rrf"] += rr
        for node in agg.values():
            if node["d_cnt"]:
                node["rrf"] += node["d_sum"] / node["d_cnt"]  # средний RR по dense
        fused = sorted(agg.values(), key=lambda x: x["rrf"], reverse=True)
        out = []
        for n in fused:
            it = dict(n["item"])
            it["rrf_score"] = n["rrf"]
            out.append(it)
        return out

    @staticmethod
    def _dedup_near(pool: list[dict], *, threshold: float = 0.9) -> list[dict]:
        """Убрать near-дубликаты в пуле (разные версии одного тома: v1/корр/финал),
        чтобы они не вытесняли разнообразие фактов из top-k. Jaccard по шинглам."""
        try:
            from ..ingest.dedup import shingles
        except Exception:  # noqa: BLE001
            return pool
        kept: list[dict] = []
        sigs: list[set] = []
        for it in pool:
            sh = shingles(it.get("text", "") or "")
            dup = False
            for prev in sigs:
                if sh and prev:
                    inter = len(sh & prev)
                    uni = len(sh | prev)
                    if uni and inter / uni >= threshold:
                        dup = True
                        break
            if not dup:
                kept.append(it)
                sigs.append(sh)
        return kept

    # ---- public API ------------------------------------------------------
    def search(self, project: str, query: str, *, top: int | None = None,
               candidates: int | None = None, sections: list[str] | None = None,
               exclude_sections: list[str] | None = None,
               use_expansion: bool | None = None,
               expansions: list[str] | None = None) -> list[dict]:
        top = top or int(self.cfg.get("retrieval.top_k", 8))
        candidates = candidates or int(self.cfg.get("retrieval.candidates", 60))
        use_expansion = self.cfg.get("retrieval.use_query_expansion", True) if use_expansion is None else use_expansion

        # expansions можно передать готовыми (батчевый путь из batch_search —
        # расширения посчитаны параллельно). Иначе считаем здесь по одному.
        if expansions is not None:
            queries = expansions or [query]
        elif use_expansion:
            queries = expand_query(query, self.cfg, n=int(self.cfg.get("retrieval.expansions", 3)))
        else:
            queries = [query]

        # прогрев кэша эмбеддингов одним батчем (иначе каждая перефраза — свой
        # отдельный прогон энкодера; batch_search это уже делает, тут — одиночный путь)
        if len(queries) > 1:
            try:
                self.embedder.embed_queries(queries)
            except Exception:  # noqa: BLE001
                pass
        lists: list[list[dict]] = []
        for q in queries:
            lists.append(self._dense(project, q, candidates=candidates,
                                     sections=sections, exclude_sections=exclude_sections))
        # Нормировка dense — ПОДОКУМЕНТНАЯ (средний reciprocal-rank по dense-спискам,
        # где документ встретился): семантика не топит единственный BM25-список,
        # но и одиночная чисто-семантическая находка не теряет вес (раньше деление
        # веса на N списков опускало её ниже худшего элемента BM25).
        dense_w = float(self.cfg.get("retrieval.dense_weight", 1.0))
        normalize = bool(self.cfg.get("retrieval.rrf_normalize_dense", True))
        weights: list[float] = [dense_w] * len(lists)
        groups: list[str] = ["dense" if normalize else "flat"] * len(lists)
        # BM25 по исходному замечанию (точные термины важнее перефраза)
        if self.cfg.get("retrieval.use_bm25", True):
            lists.append(self._bm25(project, query, candidates=candidates,
                                    sections=sections, exclude_sections=exclude_sections))
            weights.append(float(self.cfg.get("retrieval.bm25_weight", 1.0)))
            groups.append("bm25")

        fused = self._rrf(lists, k=int(self.cfg.get("retrieval.rrf_k", 60)),
                          weights=weights, groups=groups)
        # опциональный near-dup фильтр (версии одного тома) — до реранка
        if self.cfg.get("retrieval.dedup_near", False):
            fused = self._dedup_near(
                fused, threshold=float(self.cfg.get("retrieval.dedup_near_threshold", 0.9)))
        pool = fused[: max(candidates, top)]

        if self.cfg.get("retrieval.use_rerank", True) and pool:
            results = self.reranker.rerank(query, pool, top=top)
        else:
            results = pool[:top]

        # расширение контекста соседями + склейка таблиц (пункт 3B)
        if self.cfg.get("retrieval.expand_context", True) and results:
            corp = self._load_corpus(project)
            results = expand_hits(
                results, corp.by_index,
                neighbors=int(self.cfg.get("retrieval.context_neighbors", 1)),
                merge_tables=bool(self.cfg.get("retrieval.merge_tables", True)),
            )
        return results

    def batch_search(self, project: str, queries: list[str], **kw) -> list[list[dict]]:
        """Подготавливает корпус один раз и ищет по списку замечаний.

        Корпус BM25 и модель эмбеддингов загружаются единожды. Дополнительно
        (оптимизация М4):
          * расширение запросов считается ПАРАЛЛЕЛЬНО для всех замечаний разом
            (expand_query_batch) вместо N последовательных вызовов LLM;
          * все уникальные запросы предварительно эмбеддятся одним батчем —
            прогрев дискового кэша, чтобы per-query поиск шёл из кэша.
        """
        self._load_corpus(project)

        use_expansion = kw.pop("use_expansion", None)
        if use_expansion is None:
            use_expansion = self.cfg.get("retrieval.use_query_expansion", True)

        # 1) параллельное расширение (или просто исходные запросы)
        if use_expansion:
            all_expansions = expand_query_batch(
                queries, self.cfg, n=int(self.cfg.get("retrieval.expansions", 3)))
        else:
            all_expansions = [[q] for q in queries]

        # 2) прогрев кэша эмбеддингов одним батчем по всем уникальным запросам
        try:
            uniq = list(dict.fromkeys(q for exp in all_expansions for q in exp))
            if uniq:
                self.embedder.embed_queries(uniq)
        except Exception:  # noqa: BLE001
            pass

        return [self.search(project, q, expansions=exp, **kw)
                for q, exp in zip(queries, all_expansions)]
