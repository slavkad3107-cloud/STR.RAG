"""Векторная база (Qdrant). Исправления:

  * размерность берётся из модели (embedder.dim), а не зашита EMBEDDING_DIM=1024
    — иначе при смене модели Qdrant получает векторы другой длины;
  * стабильные ID чанков (приходят из chunking.build_chunks);
  * корректное закрытие клиента через atexit — это убирает ошибку при выходе
    «Exception ignored in QdrantClient.__del__ … sys.meta_path is None»;
  * два режима: embedded (path, по умолчанию) и server (url, Docker) — для
    больших баз рекомендуется server.

База лежит в каталоге данных (qdrant_dir()), отдельно от приложения.
"""
from __future__ import annotations

import atexit
import time

from ..config import Config
from ..paths import qdrant_dir, slugify

_CLIENTS: list = []


def _register_close(client) -> None:
    _CLIENTS.append(client)


@atexit.register
def _close_all() -> None:  # вызывается при нормальном завершении процесса
    for c in _CLIENTS:
        try:
            c.close()
        except Exception:
            pass
    _CLIENTS.clear()


def release_leaked_clients() -> int:
    """СБРОС «база занята» внутри ЭТОГО процесса: закрыть клиенты, которых
    не отпустил упавший на середине поиск (частая причина «already accessed»
    без всякой индексации — просьба пользователя «сброс при поиске ответов»).
    Возвращает число закрытых. Чужие процессы не трогаем — это опасно."""
    n = 0
    for c in list(_CLIENTS):
        try:
            c.close()
            n += 1
        except Exception:  # noqa: BLE001
            pass
    _CLIENTS.clear()
    return n


def collection_name(project: str) -> str:
    return f"pmoos_{slugify(project)}"


class VectorStore:
    def __init__(self, cfg: Config, dim):
        """dim: int ЛИБО callable() -> int (ленивое разрешение).

        dim нужен только ensure_collection (создание коллекции). Callable
        позволяет HybridRetriever НЕ грузить эмбеддер ~2.3 ГБ при создании —
        в поисковом пути коллекция уже существует и dim не запрашивается."""
        self.cfg = cfg
        self._dim = dim
        self._client = None

    @property
    def dim(self) -> int:
        if callable(self._dim):
            self._dim = int(self._dim())
        return self._dim

    def client(self):
        if self._client is not None:
            return self._client
        from qdrant_client import QdrantClient
        mode = self.cfg.get("qdrant.mode", "embedded")
        if mode == "server":
            url = self.cfg.get("qdrant.url", "http://localhost:6333")
            self._client = QdrantClient(url=url, timeout=60)
        else:
            last_err = None
            import warnings as _w
            for attempt in range(3):
                try:
                    with _w.catch_warnings():
                        # штатный режим: >20k точек в embedded работает нормально
                        # для нашего объёма; предупреждение лишь пугает в консоли
                        _w.filterwarnings("ignore", message="Local mode is not recommended")
                        self._client = QdrantClient(path=str(qdrant_dir()))
                    break
                except RuntimeError as e:
                    if "already accessed" in str(e):
                        last_err = e
                        # АВТОСБРОС: замок мог остаться от нашего же упавшего
                        # поиска — закрываем неотпущенные клиенты этого процесса
                        # и пробуем снова (чужие процессы не трогаем)
                        if attempt == 0:
                            freed = release_leaked_clients()
                            if freed:
                                print(f"[vectorstore] сброшено неотпущенных "
                                      f"подключений: {freed}", flush=True)
                        time.sleep(1.5)
                        continue
                    raise
            else:
                raise RuntimeError(
                    "Локальная база Qdrant занята другим процессом. Частые причины: "
                    "1) открыто ВТОРОЕ окно приложения — два СТРОЙРАГ.bat, например старая "
                    "и новая папка релиза: закройте лишнее окно; 2) идёт фоновая "
                    "индексация (Модуль 2) — дождитесь окончания или нажмите «⏹ Стоп»; "
                    "3) если ничего из этого — перезапустите приложение (СТРОЙРАГ.bat)."
                ) from last_err
        _register_close(self._client)
        return self._client

    def close(self) -> None:
        """Освободить локальную базу: embedded-Qdrant пускает только ОДИН процесс,
        поэтому после поиска (М4) клиент нужно закрывать — иначе индексация (М2)
        не сможет открыть хранилище («already accessed»)."""
        if self._client is None:
            return
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            _CLIENTS.remove(self._client)
        except ValueError:
            pass
        self._client = None

    def drop_collection(self, project: str) -> bool:
        """Полностью удалить коллекцию проекта (для переиндексации «с нуля» —
        напр. при смене режима чанкинга). Возвращает True, если была удалена."""
        c = self.client()
        name = collection_name(project)
        existing = {col.name for col in c.get_collections().collections}
        if name not in existing:
            return False
        try:
            c.delete_collection(collection_name=name)
            return True
        except Exception:  # noqa: BLE001
            return False

    def ensure_collection(self, project: str) -> None:
        from qdrant_client.http import models as qm
        c = self.client()
        name = collection_name(project)
        existing = {col.name for col in c.get_collections().collections}
        if name not in existing:
            c.create_collection(
                collection_name=name,
                vectors_config=qm.VectorParams(size=self.dim, distance=qm.Distance.COSINE),
            )
            # payload-индексы нужны только server-Qdrant (ускоряют фильтрацию).
            # Встроенный (embedded) Qdrant их игнорирует и засоряет журнал
            # предупреждением UserWarning — поэтому в локальном режиме пропускаем.
            if self.cfg.get("qdrant.mode", "embedded") == "server":
                for field in ("project", "section", "file", "doc_sha"):
                    try:
                        c.create_payload_index(
                            collection_name=name, field_name=field,
                            field_schema=qm.PayloadSchemaType.KEYWORD,
                        )
                    except Exception:
                        pass
            else:
                print("[vectorstore] локальный Qdrant: payload-индексы не нужны — пропущены",
                      flush=True)

    def existing_doc_shas(self, project: str) -> set[str]:
        """Список уже проиндексированных отпечатков документов (для дедупликации)."""
        from qdrant_client.http import models as qm
        c = self.client()
        name = collection_name(project)
        existing = {col.name for col in c.get_collections().collections}
        if name not in existing:
            return set()
        shas: set[str] = set()
        offset = None
        while True:
            points, offset = c.scroll(
                collection_name=name, with_payload=["doc_sha"],
                with_vectors=False, limit=1000, offset=offset,
            )
            for p in points:
                sha = (p.payload or {}).get("doc_sha")
                if sha:
                    shas.add(sha)
            if offset is None:
                break
        return shas

    def upsert_chunks(self, project: str, chunks: list[dict], vectors) -> None:
        from qdrant_client.http import models as qm
        c = self.client()
        name = collection_name(project)
        # точки строим ЛЕНИВО по батчам: материализация всего файла разом давала
        # сотни МБ пикового RAM на больших томах (тысячи чанков × вектор 1024)
        for i in range(0, len(chunks), 128):
            batch = [
                qm.PointStruct(id=ch["id"], vector=vectors[j].tolist(),
                               payload={**ch["payload"], "text": ch["text"]})
                for j, ch in enumerate(chunks[i:i + 128], start=i)
            ]
            c.upsert(collection_name=name, points=batch)

    def delete_by_file(self, project: str, file_rel: str) -> None:
        from qdrant_client.http import models as qm
        c = self.client()
        c.delete(
            collection_name=collection_name(project),
            points_selector=qm.FilterSelector(filter=qm.Filter(
                must=[qm.FieldCondition(key="file", match=qm.MatchValue(value=file_rel))]
            )),
        )

    def search(self, project: str, query_vector, *, top: int = 8,
               sections: list[str] | None = None,
               exclude_sections: list[str] | None = None) -> list[dict]:
        from qdrant_client.http import models as qm
        c = self.client()
        must = []
        if sections:
            must.append(qm.FieldCondition(key="section", match=qm.MatchAny(any=list(sections))))
        must_not = []
        if exclude_sections:
            must_not.append(qm.FieldCondition(key="section", match=qm.MatchAny(any=list(exclude_sections))))
        flt = qm.Filter(must=must or None, must_not=must_not or None) if (must or must_not) else None
        if hasattr(c, "query_points"):
            # qdrant-client >= 1.13: метод search удалён — используем query_points
            res = c.query_points(
                collection_name=collection_name(project), query=query_vector.tolist(),
                limit=top, query_filter=flt, with_payload=True,
            )
            hits = list(res.points)
        else:  # старые версии qdrant-client
            hits = c.search(
                collection_name=collection_name(project), query_vector=query_vector.tolist(),
                limit=top, query_filter=flt, with_payload=True,
            )
        return [{"id": h.id, "score": float(h.score), "text": (h.payload or {}).get("text", ""),
                 "payload": h.payload or {}} for h in hits]

    def count(self, project: str) -> int:
        c = self.client()
        try:
            return int(c.count(collection_name=collection_name(project)).count)
        except Exception:
            return 0
