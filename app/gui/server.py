# -*- coding: utf-8 -*-
"""СТРОЙ.RAG GUI — сервер на ЧИСТОЙ стандартной библиотеке (как в ЭКО.DOC).

Никаких Streamlit/Flask/React: http.server.ThreadingHTTPServer слушает ТОЛЬКО
127.0.0.1:8747, отдаёт JSON-API и одну страницу index.html (ванильный
HTML/CSS/JS, полностью оффлайн, без CDN). Тяжёлая работа — обычные модули
pmoos/*; GUI лишь тонкая обёртка, та же логика доступна из CLI (modules/*.py).

Запуск: СТРОЙРАГ.bat  →  python app/gui/server.py  →  открывается браузер.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

HOST, PORT = "127.0.0.1", 8747
_HTML = Path(__file__).with_name("index.html")

# фоновые задачи в процессе сервера (сбор показателей): project → {running, error}
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


def _cfg():
    from pmoos.config import load_config
    return load_config()


def _ot(project: str) -> str:
    try:
        from pmoos.index.indexer import read_state
        return read_state(project).get("object_type") or "площадной"
    except Exception:  # noqa: BLE001
        return "площадной"


# ─────────────────────────── API-обработчики ────────────────────────────────
def api_meta(q, body):
    from pmoos import VERSION
    from pmoos.projects import list_projects
    from pmoos.ingest.sections import target_sections
    cfg = _cfg()
    prov = cfg.default_provider()
    return {
        "version": VERSION,
        "projects": list_projects(),
        "targets": [{"code": t["code"], "label": f"{t['short']} — {t['name']}"}
                    for t in target_sections()],
        "target": str(cfg.get("target_section", "OOS") or "OOS"),
        "model": f"{prov} · {cfg.model_for(prov, 'answer') or '—'}",
    }


def api_project_new(q, body):
    from pmoos.projects import register_project
    name = str(body.get("name", "")).strip()
    if not name:
        raise ValueError("пустое имя проекта")
    register_project(name)
    return {"ok": True, "project": name}


def api_info(q, body):
    from pmoos.ingest.uploads import list_uploads
    from pmoos.paths import project_paths
    p = q["project"]
    org, ot = "", _ot(p)
    try:
        from pmoos.ingest.inventory import load_inventory
        inv = load_inventory(p) or {}
        org = inv.get("organization", "")
        ot = inv.get("object_type") or ot
    except Exception:  # noqa: BLE001
        pass
    rd = project_paths(p)["remarks_dir"]
    remarks = sorted(f.name for f in rd.iterdir() if f.is_file()) if rd.exists() else []
    return {"organization": org, "object_type": ot,
            "uploads": list_uploads(p), "remarks": remarks}


def api_org(q, body):
    from pmoos.ingest.inventory import load_inventory
    from pmoos.paths import project_paths
    p = body["project"]
    inv = load_inventory(p) or {}
    inv["organization"] = str(body.get("organization", "")).strip()
    pp = project_paths(p)["inventory"]
    pp.parent.mkdir(parents=True, exist_ok=True)
    pp.write_text(json.dumps(inv, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True}


def api_target(q, body):
    cfg = _cfg()
    cfg.set("target_section", str(body.get("code", "OOS")))
    cfg.save()
    return {"ok": True}


def api_ai_task(q, body):
    """Доп. задание ИИ (ТЗ 27.07) — одно на модуль ответов."""
    cfg = _cfg()
    if "value" in body:
        cfg.set("ai.modules.module4.instruction", str(body.get("value", "")).strip())
        cfg.save()
        return {"ok": True}
    return {"value": str(cfg.get("ai.modules.module4.instruction", "") or "")}


def api_upload(q, body_bytes):
    from pmoos.ingest.uploads import save_bytes
    name = urllib.parse.unquote(q.get("name", "файл.bin"))
    saved = save_bytes(q["project"], name, body_bytes)
    return {"saved": saved}


def api_fetch_url(q, body):
    from pmoos.ingest.remarks_fetch import fetch_to
    from pmoos.paths import project_paths
    p = fetch_to(str(body["url"]).strip(), project_paths(body["project"])["uploads"])
    if p.suffix.lower() == ".zip":                 # архив по ссылке — распаковываем
        from pmoos.ingest.uploads import unpack_zip_path
        saved = unpack_zip_path(body["project"], p)  # с диска, без RAM×2 (ревью)
        p.unlink(missing_ok=True)
        return {"saved": saved}
    return {"saved": [p.name]}


def api_copy_folder(q, body):
    from pmoos.ingest.uploads import copy_folder
    return {"copied": copy_folder(body["project"], body["path"])}


def api_index(q, body):
    from pmoos.index import indexer
    p, action = body["project"], body.get("action", "start")
    if action in ("start", "reindex"):
        with _JOBS_LOCK:
            if (_JOBS.get(p) or {}).get("running"):
                raise RuntimeError("идёт сбор показателей (десятки секунд) — "
                                   "запустите индексацию после его завершения")
        pid = indexer.start_background(p, object_type=body.get("object_type") or _ot(p),
                                       reindex=(action == "reindex"))
        return {"pid": pid}
    if action == "pause":
        indexer.request_pause(p)
    elif action == "stop":
        indexer.stop_indexing(p)
    return {"ok": True}


def api_index_state(q, body):
    from pmoos.index.indexer import read_state
    st = read_state(q["project"])
    return {k: st.get(k) for k in ("status", "message", "total_files", "done_files",
                                   "total_chunks", "object_type", "current_file")}


def api_registry(q, body):
    from pmoos.data import registry as R
    p = q["project"]
    reg = R.load_registry(p)
    inds = reg.get("indicators") or {}
    rows = []
    for m in R.INDICATORS:
        rec = inds.get(m["key"], {})
        prov = rec.get("provenance") or {}
        rows.append({
            "key": m["key"], "label": m["label"],
            "value": rec.get("value", ""), "unit": rec.get("unit", m["unit"]),
            "source": f"{prov.get('file', '')} {prov.get('loc', '')}".strip(),
            "how": {"manual": "вручную", "chosen": "выбрано",
                    "auto": "из документа"}.get(rec.get("source", ""), ""),
            "conflict": bool(rec.get("conflict")),
            "has_scan": bool(rec.get("scan")),
            "variants": [{"value": v["value"], "unit": v.get("unit", ""),
                          "count": v.get("count", 0),
                          "src": "; ".join(f"{s.get('file','')} {s.get('loc','')}"
                                           for s in v.get("sources", [])[:2])}
                         for v in (rec.get("variants") or [])[:8]],
        })
    with _JOBS_LOCK:
        job = dict(_JOBS.get(p) or {})
    return {"rows": rows, "job": job,
            "scanned_chunks": reg.get("scanned_chunks", 0)}


def api_collect(q, body):
    p = body["project"]
    # взаимная блокировка с индексацией (ревью): embedded-Qdrant однопроцессный
    from pmoos.index.indexer import is_running as _idx_running
    if _idx_running(p):
        raise RuntimeError("идёт индексация — показатели соберутся сами по её "
                           "завершении (или дождитесь конца и повторите)")
    with _JOBS_LOCK:
        if (_JOBS.get(p) or {}).get("running"):
            return {"ok": False, "error": "сбор уже идёт"}
        _JOBS[p] = {"running": True, "error": ""}

    def run():
        err = ""
        try:
            from pmoos.data.registry import extract_from_index
            extract_from_index(p, _cfg())
        except Exception as e:  # noqa: BLE001
            err = str(e)[:300]
        with _JOBS_LOCK:
            _JOBS[p] = {"running": False, "error": err}
    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


def api_reg_set(q, body):
    from pmoos.data.registry import set_value
    set_value(body["project"], body["key"], str(body["value"]))
    return {"ok": True}


def api_reg_choose(q, body):
    from pmoos.data.registry import choose_variant
    choose_variant(body["project"], body["key"], str(body["value"]))
    return {"ok": True}


def api_scan(q, body):
    """PNG листа-источника: сохранённый снимок или живой рендер."""
    from pmoos.data import registry as R
    from pmoos.paths import project_paths
    p, key = q["project"], q["key"]
    rec = (R.load_registry(p).get("indicators") or {}).get(key, {})
    if rec.get("scan"):
        sp = project_paths(p)["root"] / rec["scan"]
        if sp.exists():
            return ("image/png", sp.read_bytes())
    prov = rec.get("provenance") or {}
    png = R.render_source_page(p, prov.get("file", ""), prov.get("loc", ""))
    if not png:
        raise FileNotFoundError("скан недоступен: исходник удалён после индексации")
    return ("image/png", png)


def api_remarks_upload(q, body_bytes):
    from pmoos.paths import project_paths
    name = Path(urllib.parse.unquote(q.get("name", "замечания.docx"))).name
    rd = project_paths(q["project"])["remarks_dir"]
    rd.mkdir(parents=True, exist_ok=True)
    dest = rd / re.sub(r'[<>:"|?*]', "_", name)
    dest.write_bytes(body_bytes)
    return {"path": str(dest), "name": dest.name}


def api_answers_ctl(q, body):
    from pmoos.pipeline import block1_answers as B
    p, action = body["project"], body.get("action")
    if action == "start":
        rp = body.get("remarks_path") or None
        if not rp:
            # после F5 браузер теряет выбор — берём СВЕЖАЙШИЙ файл замечаний
            from pmoos.paths import project_paths
            rd = project_paths(p)["remarks_dir"]
            cand = sorted((f for f in rd.iterdir() if f.is_file()),
                          key=lambda f: f.stat().st_mtime,
                          reverse=True) if rd.exists() else []
            if cand:
                rp = str(cand[0])
        pid = B.start_answers_background(p, object_type=_ot(p), remarks_path=rp)
        return {"pid": pid, "remarks": Path(rp).name if rp else ""}
    if action == "stop":
        B.stop_answers(p, hard=bool(body.get("hard")))
    return {"ok": True}


def api_answers_state(q, body):
    from pmoos.pipeline.block1_answers import read_answers_state
    st = read_answers_state(q["project"])
    return {k: st.get(k) for k in ("status", "message", "total", "done")}


def api_answers(q, body):
    from pmoos.pipeline.block1_answers import load_answers
    out = []
    for a in (load_answers(q["project"]) or {}).get("answers", []):
        out.append({k: a.get(k, "") for k in (
            "number", "remark", "answer", "status", "category", "oos_volume",
            "edit_location", "edit_was", "edit_shall", "missing_data",
            "correction")}
            | {"attachments": a.get("attachments") or [],
               "sources": [{"file": s.get("file", ""), "loc": s.get("loc", "")}
                           for s in (a.get("sources") or [])[:6]]})
    return {"answers": out}


def api_decide(q, body):
    from pmoos.pipeline.block1_answers import set_decisions
    nums = [str(n) for n in body.get("numbers") or []]
    st = str(body.get("status", "accepted"))
    ua = body.get("user_answer") or None
    set_decisions(body["project"],
                  [{"number": n, "status": st, "user_answer": ua} for n in nums])
    return {"ok": True, "count": len(nums)}


def api_export(q, body):
    p, kind = body["project"], body.get("kind")
    if kind == "answers_docx":
        from pmoos.output.answers_table import build_answers_table_docx as f
    elif kind == "answers_xlsx":
        from pmoos.output.answers_table import build_answers_table_xlsx as f
    elif kind == "changes":
        from pmoos.output.changes_table import build_changes_xlsx as f
    elif kind == "gaps":
        from pmoos.output.gaps import build_gaps_xlsx as f
    elif kind == "draft":
        from pmoos.output.section_draft import build_section_draft
        tgt = str(_cfg().get("target_section", "OOS") or "OOS")
        return {"path": str(build_section_draft(p, tgt))}
    elif kind == "uprza_out":
        from pmoos.output.uprza_export import build_uprza_export
        return {"paths": {k: str(v) for k, v in build_uprza_export(p).items()}}
    else:
        raise ValueError(f"неизвестная выгрузка: {kind}")
    return {"path": str(f(p))}


def _corr_dir(project: str) -> Path:
    """Исходные тома для корректировки живут ОТДЕЛЬНО от tmp_uploads: их не
    трогают ни индексация, ни «Очистить временные файлы»."""
    from pmoos.paths import project_paths
    d = project_paths(project)["root"] / "corr_sources"
    d.mkdir(parents=True, exist_ok=True)
    return d


def api_corr_upload(q, body_bytes):
    """Загрузка ИСХОДНОГО тома раздела (.docx) для внесения правок."""
    name = Path(urllib.parse.unquote(q.get("name", "том.docx"))).name
    if not name.lower().endswith(".docx"):
        raise ValueError("нужен .docx (старый .doc пересохраните в Word как .docx)")
    dest = _corr_dir(q["project"]) / re.sub(_BAD_RX, "_", name)
    dest.write_bytes(body_bytes)
    return {"saved": dest.name}


def api_corr_list(q, body):
    d = _corr_dir(q["project"])
    return {"volumes": [{"name": f.name, "kb": f.stat().st_size // 1024}
                        for f in sorted(d.glob("*.docx"))]}


def api_corr_preview(q, body):
    """DRY-RUN: что и куда встанет — замены по месту / по якорю / в конец."""
    from pmoos.output.docx_writer import preview_corrections
    srcs = sorted(_corr_dir(body["project"]).glob("*.docx"))
    if not srcs:
        raise FileNotFoundError("сначала загрузите исходные тома (.docx)")
    return preview_corrections(body["project"], [str(s) for s in srcs])


def api_corr_write(q, body):
    """СФОРМИРОВАТЬ откорректированный раздел: *_КОРР.docx с правками
    («было»→«стало» по месту с жёлтой заливкой; остальное — по якорям)."""
    from pmoos.output.docx_writer import write_corrected_volumes
    p = body["project"]
    srcs = sorted(_corr_dir(p).glob("*.docx"))
    if not srcs:
        raise FileNotFoundError("сначала загрузите исходные тома (.docx)")
    outs, failed = write_corrected_volumes(p, [str(s) for s in srcs])
    return {"outputs": [str(o) for o in outs],
            "names": [o.name for o in outs], "failed": failed}


def api_corr_delete(q, body):
    d = _corr_dir(body["project"])
    t = d / Path(str(body.get("name", ""))).name
    if t.exists():
        t.unlink()
    return {"ok": True}


def api_uprza_import(q, body_bytes):
    from pmoos.output.uprza_import import import_uprza_results
    from pmoos.paths import project_paths
    name = Path(urllib.parse.unquote(q.get("name", "результаты.csv"))).name
    tmp = project_paths(q["project"])["out"] / f"уприза_вход_{re.sub(_BAD_RX, '_', name)}"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(body_bytes)
    r = import_uprza_results(q["project"], tmp)
    return {"rows": len(r["rows"]), "exceedances": r["exceedances"][:10]}


def api_uprza(q, body):
    from pmoos.output.uprza_import import load_uprza_results
    return load_uprza_results(q["project"]) or {"rows": []}


def api_open(q, body):
    """Открыть файл/папку в проводнике — локальное приложение, как в ЭКО.DOC."""
    import os
    from pmoos.paths import data_root
    p = Path(body["path"]).resolve()
    root = data_root().resolve()
    if root not in p.parents and p != root:      # только внутри данных приложения
        raise PermissionError("путь вне каталога данных")
    os.startfile(p)
    return {"ok": True}


def api_health(q, body):
    from pmoos.core.health import read_health, rank_working, TIER_RU, TIERS
    h = read_health()
    return {"providers": [
        {"name": p, "tier": TIER_RU.get(TIERS.get(p, 9), "?"),
         "model": (r or {}).get("best_model", ""), "ms": (r or {}).get("ms")}
        for p, r in rank_working(h)]}


_BAD_RX = re.compile(r'[<>:"|?*]')

# роутинг: имя → (обработчик, «сырое тело»)
ROUTES_JSON = {
    "meta": api_meta, "project_new": api_project_new, "info": api_info,
    "org": api_org, "target": api_target, "ai_task": api_ai_task,
    "fetch_url": api_fetch_url, "copy_folder": api_copy_folder,
    "index": api_index, "index_state": api_index_state,
    "registry": api_registry, "collect": api_collect,
    "reg_set": api_reg_set, "reg_choose": api_reg_choose,
    "answers_ctl": api_answers_ctl, "answers_state": api_answers_state,
    "answers": api_answers, "decide": api_decide, "export": api_export,
    "uprza": api_uprza, "open": api_open, "health": api_health,
    "corr_list": api_corr_list, "corr_preview": api_corr_preview,
    "corr_write": api_corr_write, "corr_delete": api_corr_delete,
}
ROUTES_RAW = {"upload": api_upload, "remarks_upload": api_remarks_upload,
              "uprza_import": api_uprza_import, "corr_upload": api_corr_upload}
ROUTES_BIN = {"scan": api_scan}


class Handler(BaseHTTPRequestHandler):
    server_version = "STRRAG"

    def log_message(self, fmt, *args):   # тихий лог в консоль сервера
        sys.stderr.write("[gui] " + fmt % args + "\n")

    def _send(self, code: int, ctype: str, data: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _route(self, method: str):
        # ЗАЩИТА ОТ DNS-REBINDING (ревью): чужой сайт, чей домен резолвится в
        # 127.0.0.1, для браузера same-origin — без проверки Host он мог бы
        # читать данные и запускать reindex. Принимаем только локальные Host.
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        if host not in ("127.0.0.1", "localhost", "[::1]", ""):
            return self._json({"error": "недопустимый Host"}, 403)
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        name = u.path.strip("/")
        if name in ("", "index.html"):
            return self._send(200, "text/html; charset=utf-8", _HTML.read_bytes())
        if not name.startswith("api/"):
            return self._send(404, "text/plain", b"not found")
        name = name[4:]
        try:
            # эндпоинты с проектом в query: пустой проект → понятная ошибка,
            # а не молчаливый «фантомный» проект (найдено ревью)
            if name in ("info", "index_state", "registry", "answers",
                        "answers_state", "uprza", "scan", "upload",
                        "remarks_upload", "uprza_import", "corr_upload",
                        "corr_list"):
                if not q.get("project", "").strip():
                    raise ValueError("не выбран объект/проект — создайте его "
                                     "кнопкой «+ Новый»")
            if name in ROUTES_BIN:
                ctype, data = ROUTES_BIN[name](q, None)
                return self._send(200, ctype, data)
            if name in ROUTES_RAW:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 3_000_000_000:
                    raise ValueError("файл слишком большой")
                raw = self.rfile.read(n) if n else b""
                return self._json(ROUTES_RAW[name](q, raw))
            if name in ROUTES_JSON:
                body = {}
                if method == "POST":
                    n = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(n) if n else b"{}"
                    try:
                        body = json.loads(raw)
                    except UnicodeDecodeError:
                        # curl в русской консоли шлёт cp1251/cp866 — не падаем
                        for enc in ("cp1251", "cp866"):
                            try:
                                body = json.loads(raw.decode(enc))
                                break
                            except (UnicodeDecodeError, json.JSONDecodeError):
                                continue
                        else:
                            raise
                return self._json(ROUTES_JSON[name](q, body))
            return self._json({"error": f"нет эндпоинта {name}"}, 404)
        except Exception as e:  # noqa: BLE001 — ошибка уходит в интерфейс
            return self._json({"error": str(e)[:400]}, 500)

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")


def startup_background():
    """Как при старте Streamlit-версии: закрыть застрявшие сеансы, подтянуть
    ключи из облака, в фоне проверить провайдеров."""
    try:
        from pmoos.core.session_guard import close_stale_sessions
        close_stale_sessions()
    except Exception:  # noqa: BLE001
        pass
    try:
        from pmoos.core.keysync import sync_keys_from_transfer
        sync_keys_from_transfer()
    except Exception:  # noqa: BLE001
        pass
    try:
        # как в прежнем hub (ревью: auto_select потерялся при переезде):
        # свежий кэш → авто-выбор живого провайдера сразу; иначе — проверка
        # всех и авто-выбор по её итогам
        from pmoos.core.health import auto_select, probe_all, read_health
        cfg = _cfg()
        h = read_health(max_age_h=24)
        if not h:
            h = probe_all(cfg)
        if h and bool(cfg.get("ai.auto_select", True)):
            auto_select(cfg, h)      # не force: живой выбор пользователя уважается
    except Exception:  # noqa: BLE001
        pass


def _ours_on(port: int) -> bool:
    """На порту действительно СТРОЙ.RAG? (ревью: чужой сервис на 8747 давал
    ложное «уже работает», и приложение молча не стартовало)."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/meta", timeout=2) as r:
            return "STR.RAG" in json.loads(r.read()).get("version", "")
    except Exception:  # noqa: BLE001
        return False


def main(open_browser: bool = True):
    global PORT
    import socket
    import webbrowser
    for port in range(PORT, PORT + 10):
        probe = socket.socket()
        probe.settimeout(0.4)
        busy = probe.connect_ex((HOST, port)) == 0
        probe.close()
        if busy:
            if _ours_on(port):
                # наш сервер уже работает — второй не нужен (Qdrant однопроцессный)
                if open_browser:
                    webbrowser.open(f"http://{HOST}:{port}/")
                print(f"[gui] сервер уже работает: http://{HOST}:{port}/", flush=True)
                return
            continue                    # порт занят ЧУЖИМ сервисом — пробуем следующий
        PORT = port
        break
    else:
        print("[gui] нет свободного порта 8747-8756", flush=True)
        return
    threading.Thread(target=startup_background, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(f"http://{HOST}:{PORT}/")).start()
    print(f"[gui] СТРОЙ.RAG: http://{HOST}:{PORT}/  (Ctrl+C — выход)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main(open_browser="--no-browser" not in sys.argv)
