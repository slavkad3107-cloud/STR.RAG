# -*- coding: utf-8 -*-
"""СТРОЙ.RAG — НАТИВНАЯ оболочка (ТЗ 31.07: «переделываем со стримлита, делаем
как в ЭКОДОК — стримлит неповоротлив»).

Как в ЭКО.DOC: tkinter/ttk — мгновенный отклик, без браузера и порта 8501,
ничего не «перерисовывается» и не сбрасывается. Весь функционал — те же модули
pmoos/*, что и у веб-версии; run.bat (Streamlit) остаётся как запасной вход.

Запуск: СТРОЙРАГ.bat (pythonw app/native.py).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from pmoos import VERSION                                    # noqa: E402
from pmoos.config import load_config                         # noqa: E402
from pmoos.paths import project_paths                        # noqa: E402
from pmoos.projects import list_projects, register_project   # noqa: E402

PAD = {"padx": 8, "pady": 4}


def _bg(fn, done=None):
    """Выполнить fn в потоке; done(result|Exception) — в главном потоке tk."""
    def run():
        try:
            r = fn()
        except Exception as e:  # noqa: BLE001
            r = e
        if done:
            _root.after(0, lambda: done(r))
    threading.Thread(target=run, daemon=True).start()


class App(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.pack(fill="both", expand=True)
        self.cfg = load_config()
        self.project = tk.StringVar()
        self.target = tk.StringVar(value=str(self.cfg.get("target_section", "OOS")))
        self._build_header()
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=6, pady=4)
        self._tabs = {}
        for name, builder in (
            ("ЗАГРУЗКА", self._tab_upload), ("БАЗА", self._tab_index),
            ("ДАННЫЕ", self._tab_data), ("ОТВЕТЫ", self._tab_answers),
            ("ВЫГРУЗКА", self._tab_export), ("УПРЗА", self._tab_uprza),
        ):
            f = ttk.Frame(self.nb)
            self.nb.add(f, text=f"  {name}  ")
            self._tabs[name] = f
            builder(f)
        self.status = ttk.Label(self, text=VERSION, anchor="w")
        self.status.pack(fill="x", padx=6)
        self._poll()

    # ── шапка: проект · организация · тип · раздел · модель ──
    def _build_header(self):
        h = ttk.Frame(self)
        h.pack(fill="x", **PAD)
        ttk.Label(h, text="Объект:").grid(row=0, column=0, sticky="w")
        self.cb_prj = ttk.Combobox(h, textvariable=self.project,
                                   values=list_projects(), width=28, state="readonly")
        self.cb_prj.grid(row=0, column=1, sticky="w", padx=4)
        self.cb_prj.bind("<<ComboboxSelected>>", lambda e: self._switch_project())
        ttk.Button(h, text="+ Новый", width=9,
                   command=self._new_project).grid(row=0, column=2, padx=2)
        ttk.Label(h, text="Раздел:").grid(row=0, column=3, sticky="e", padx=(16, 2))
        from pmoos.ingest.sections import target_sections
        self._targets = {t["code"]: f"{t['short']} — {t['name'][:44]}"
                         for t in target_sections()}
        self.cb_tgt = ttk.Combobox(h, values=list(self._targets.values()),
                                   width=52, state="readonly")
        self.cb_tgt.grid(row=0, column=4, sticky="w")
        cur = self.target.get()
        if cur in self._targets:
            self.cb_tgt.set(self._targets[cur])
        self.cb_tgt.bind("<<ComboboxSelected>>", lambda e: self._switch_target())
        self.lbl_info = ttk.Label(h, text="", foreground="#555")
        self.lbl_info.grid(row=1, column=0, columnspan=5, sticky="w")
        prjs = list_projects()
        if prjs and not self.project.get():
            self.project.set(prjs[0])
            self._refresh_info()

    def _new_project(self):
        name = simpledialog.askstring("Новый объект", "Название объекта/проекта:")
        if name:
            register_project(name.strip())
            self.cb_prj["values"] = list_projects()
            self.project.set(name.strip())
            self._refresh_info()

    def _switch_project(self):
        # подтверждение смены объекта (ТЗ 27.07) — данные меняются во всех вкладках
        if not messagebox.askyesno("Смена объекта",
                                   f"Перейти к объекту «{self.project.get()}»?\n"
                                   f"Данные во всех вкладках сменятся."):
            return
        self._refresh_info()
        self._refresh_data_table()
        self._refresh_answers()

    def _switch_target(self):
        for code, label in self._targets.items():
            if label == self.cb_tgt.get():
                self.cfg.set("target_section", code)
                self.cfg.save()
                self.target.set(code)
                break

    def _refresh_info(self):
        p = self.project.get()
        org, ot = "", ""
        try:
            from pmoos.ingest.inventory import load_inventory
            inv = load_inventory(p) or {}
            org = inv.get("organization", "")
            ot = inv.get("object_type", "")
        except Exception:  # noqa: BLE001
            pass
        if not ot:
            try:
                from pmoos.index.indexer import read_state
                ot = read_state(p).get("object_type", "")
            except Exception:  # noqa: BLE001
                pass
        prov = self.cfg.default_provider()
        mdl = self.cfg.model_for(prov, "answer")
        self.lbl_info.config(text=f"🏢 {org or 'организация не указана'} · "
                                  f"📐 тип: {ot or 'площадной'} (зафиксирован) · "
                                  f"🤖 единая модель: {prov} · {mdl}")

    def _ot(self) -> str:
        try:
            from pmoos.index.indexer import read_state
            return read_state(self.project.get()).get("object_type") or "площадной"
        except Exception:  # noqa: BLE001
            return "площадной"

    # ── ЗАГРУЗКА ──
    def _tab_upload(self, f):
        ttk.Label(f, text="Файлы ПД: pdf/docx/xlsx/jpg/png/tif/xml/zip. "
                          "Папку или архив можно целиком.").pack(anchor="w", **PAD)
        row = ttk.Frame(f); row.pack(anchor="w", **PAD)
        ttk.Button(row, text="📥 Добавить файлы…",
                   command=self._add_files).pack(side="left", padx=2)
        ttk.Button(row, text="📂 Забрать из папки…",
                   command=self._add_folder).pack(side="left", padx=2)
        ttk.Button(row, text="🌐 По ссылке…",
                   command=self._add_url).pack(side="left", padx=2)
        ttk.Button(row, text="🗂 Открыть папку загрузок",
                   command=lambda: os.startfile(
                       project_paths(self.project.get())["uploads"])).pack(side="left", padx=2)
        self.lst_files = tk.Listbox(f, height=14)
        self.lst_files.pack(fill="both", expand=True, **PAD)
        org_row = ttk.Frame(f); org_row.pack(anchor="w", **PAD)
        ttk.Label(org_row, text="🏢 Организация:").pack(side="left")
        self.ent_org = ttk.Entry(org_row, width=48)
        self.ent_org.pack(side="left", padx=4)
        ttk.Button(org_row, text="Сохранить", command=self._save_org).pack(side="left")
        self._refresh_uploads()

    def _refresh_uploads(self):
        self.lst_files.delete(0, "end")
        up = project_paths(self.project.get())["uploads"]
        if up.exists():
            for p in sorted(up.iterdir()):
                if p.is_file():
                    self.lst_files.insert("end", f"{p.name}   ({p.stat().st_size // 1024} КБ)")

    def _add_files(self):
        from pmoos.ingest.loaders import SUPPORTED_EXT
        paths = filedialog.askopenfilenames(title="Файлы ПД")
        if not paths:
            return
        import shutil
        import zipfile
        up = project_paths(self.project.get())["uploads"]
        up.mkdir(parents=True, exist_ok=True)
        n = 0
        for sp in paths:
            sp = Path(sp)
            if sp.suffix.lower() == ".zip":
                with zipfile.ZipFile(sp) as zf:
                    for zi in zf.infolist():
                        if zi.is_dir() or Path(zi.filename).suffix.lower() not in SUPPORTED_EXT:
                            continue
                        try:
                            nm = zi.filename.encode("cp437").decode("cp866")
                        except Exception:  # noqa: BLE001
                            nm = zi.filename
                        parts = nm.replace("\\", "/").strip("/").split("/")
                        if "__MACOSX" in parts or parts[-1].startswith("._"):
                            continue
                        import re as _re
                        flat = _re.sub(r'[<>:"|?*]', "_", "__".join(parts))
                        with zf.open(zi) as src, open(up / flat, "wb") as dst:
                            shutil.copyfileobj(src, dst, 1 << 20)
                        n += 1
            elif sp.suffix.lower() in SUPPORTED_EXT:
                shutil.copy2(sp, up / sp.name)
                n += 1
        self._refresh_uploads()
        self.status.config(text=f"Добавлено файлов: {n}. Дальше — вкладка БАЗА.")

    def _add_folder(self):
        from pmoos.ingest.loaders import SUPPORTED_EXT
        d = filedialog.askdirectory(title="Папка с ПД")
        if not d:
            return
        import re as _re
        import shutil
        src = Path(d)
        up = project_paths(self.project.get())["uploads"]
        up.mkdir(parents=True, exist_ok=True)
        n = 0
        for p in sorted(src.rglob("*")):
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXT:
                flat = _re.sub(r'[<>:"|?*]', "_", "__".join(p.relative_to(src).parts))
                try:
                    shutil.copy2(p, up / flat)
                    n += 1
                except OSError:
                    continue
        self._refresh_uploads()
        self.status.config(text=f"Скопировано файлов: {n}.")

    def _add_url(self):
        url = simpledialog.askstring("По ссылке", "Ссылка на файл (https / Google Drive):")
        if not url:
            return
        def job():
            from pmoos.ingest.remarks_fetch import fetch_to
            return fetch_to(url.strip(), project_paths(self.project.get())["uploads"])
        def done(r):
            if isinstance(r, Exception):
                messagebox.showerror("Ошибка", f"Не удалось скачать: {r}")
            else:
                self._refresh_uploads()
                self.status.config(text=f"Скачан: {r.name}")
        self.status.config(text="Скачивание…")
        _bg(job, done)

    def _save_org(self):
        try:
            from pmoos.ingest.inventory import load_inventory
            p = self.project.get()
            inv = load_inventory(p) or {}
            inv["organization"] = self.ent_org.get().strip()
            pp = project_paths(p)["inventory"]
            pp.parent.mkdir(parents=True, exist_ok=True)
            pp.write_text(json.dumps(inv, ensure_ascii=False, indent=2), encoding="utf-8")
            self._refresh_info()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Ошибка", str(e))

    # ── БАЗА ──
    def _tab_index(self, f):
        row = ttk.Frame(f); row.pack(anchor="w", **PAD)
        ttk.Button(row, text="▶ Индексировать",
                   command=lambda: self._idx(False)).pack(side="left", padx=2)
        ttk.Button(row, text="♻ Переиндексировать заново",
                   command=lambda: self._idx(True)).pack(side="left", padx=2)
        ttk.Button(row, text="⏸ Пауза", command=self._idx_pause).pack(side="left", padx=2)
        ttk.Button(row, text="⏹ Стоп", command=self._idx_stop).pack(side="left", padx=2)
        self.pb_idx = ttk.Progressbar(f, maximum=100)
        self.pb_idx.pack(fill="x", **PAD)
        self.lbl_idx = ttk.Label(f, text="—", wraplength=900, justify="left")
        self.lbl_idx.pack(anchor="w", **PAD)
        ttk.Label(f, text="Найденные данные (после индексации — подробно во вкладке "
                          "ДАННЫЕ):").pack(anchor="w", **PAD)
        self.tv_found = ttk.Treeview(f, columns=("v", "s"), show="tree headings", height=8)
        self.tv_found.heading("#0", text="Показатель")
        self.tv_found.heading("v", text="Значение")
        self.tv_found.heading("s", text="Источник")
        self.tv_found.column("#0", width=280)
        self.tv_found.column("v", width=140)
        self.tv_found.column("s", width=420)
        self.tv_found.pack(fill="both", expand=True, **PAD)

    def _idx(self, reindex: bool):
        if reindex and not messagebox.askyesno(
                "Переиндексация", "Стереть базу проекта и переиндексировать заново?\n"
                "(нужно после обновления с починкой распознавания)"):
            return
        from pmoos.index.indexer import start_background
        pid = start_background(self.project.get(), object_type=self._ot(),
                               reindex=reindex)
        self.status.config(text=f"Индексация запущена (pid {pid}).")

    def _idx_pause(self):
        from pmoos.index.indexer import request_pause
        request_pause(self.project.get())

    def _idx_stop(self):
        from pmoos.index.indexer import stop_indexing
        stop_indexing(self.project.get())

    # ── ДАННЫЕ ──
    def _tab_data(self, f):
        row = ttk.Frame(f); row.pack(anchor="w", **PAD)
        ttk.Button(row, text="🔍 Собрать показатели из базы",
                   command=self._collect_data).pack(side="left", padx=2)
        ttk.Button(row, text="✏️ Ввести значение…",
                   command=self._edit_value).pack(side="left", padx=2)
        ttk.Button(row, text="✔ Выбрать вариант…",
                   command=self._pick_variant).pack(side="left", padx=2)
        ttk.Button(row, text="🖼 Лист-источник",
                   command=self._show_scan).pack(side="left", padx=2)
        self.tv_data = ttk.Treeview(
            f, columns=("v", "u", "src", "how", "c"), show="tree headings")
        for col, txt, w in (("#0", "Показатель", 260), ("v", "Значение", 120),
                            ("u", "Ед.", 70), ("src", "Откуда", 330),
                            ("how", "Способ", 100), ("c", "⚠", 90)):
            self.tv_data.heading(col, text=txt)
            self.tv_data.column(col, width=w)
        self.tv_data.pack(fill="both", expand=True, **PAD)
        self._refresh_data_table()

    def _refresh_data_table(self):
        from pmoos.data import registry as R
        self.tv_data.delete(*self.tv_data.get_children())
        reg = R.load_registry(self.project.get())
        inds = reg.get("indicators") or {}
        for m in R.INDICATORS:
            rec = inds.get(m["key"], {})
            prov = rec.get("provenance") or {}
            how = {"manual": "вручную", "chosen": "выбрано",
                   "auto": "из документа"}.get(rec.get("source", ""), "—")
            self.tv_data.insert(
                "", "end", iid=m["key"], text=m["label"],
                values=(rec.get("value", "") or "—", rec.get("unit", m["unit"]),
                        f"{prov.get('file', '')} {prov.get('loc', '')}".strip() or "—",
                        how, "расхождение" if rec.get("conflict") else ""))

    def _collect_data(self):
        p = self.project.get()
        self.status.config(text="Читаю проиндексированные тома… (десятки секунд)")
        def done(r):
            if isinstance(r, Exception):
                messagebox.showerror("Ошибка", f"Не удалось собрать: {r}\n"
                                     f"База занята индексацией? Дождитесь её конца.")
            else:
                self._refresh_data_table()
                self.status.config(text="Показатели собраны.")
        _bg(lambda: __import__("pmoos.data.registry", fromlist=["x"])
            .extract_from_index(p, load_config()), done)

    def _sel_key(self):
        sel = self.tv_data.selection()
        if not sel:
            messagebox.showinfo("Данные", "Выберите показатель в таблице.")
            return None
        return sel[0]

    def _edit_value(self):
        key = self._sel_key()
        if not key:
            return
        from pmoos.data import registry as R
        val = simpledialog.askstring("Ввод значения",
                                     f"{R._BY_KEY[key]['label']} ({R._BY_KEY[key]['unit']}):")
        if val and val.strip():
            R.set_value(self.project.get(), key, val.strip())
            self._refresh_data_table()

    def _pick_variant(self):
        key = self._sel_key()
        if not key:
            return
        from pmoos.data import registry as R
        rec = (R.load_registry(self.project.get()).get("indicators") or {}).get(key, {})
        vars_ = rec.get("variants") or []
        if len(vars_) < 2:
            messagebox.showinfo("Данные", "Расхождений по этому показателю нет.")
            return
        win = tk.Toplevel(self)
        win.title(f"Выбор значения — {rec.get('label', key)}")
        for v in vars_[:8]:
            srcs = "; ".join(f"{s.get('file','')} {s.get('loc','')}"
                             for s in v.get("sources", [])[:2])
            def choose(val=v["value"]):
                R.choose_variant(self.project.get(), key, val)
                win.destroy()
                self._refresh_data_table()
            ttk.Button(win, text=f"{v['value']} {v['unit']}  ·  {v['count']}×  ·  {srcs}",
                       command=choose).pack(fill="x", padx=8, pady=3)

    def _show_scan(self):
        key = self._sel_key()
        if not key:
            return
        from pmoos.data import registry as R
        p = self.project.get()
        rec = (R.load_registry(p).get("indicators") or {}).get(key, {})
        sp = project_paths(p)["root"] / str(rec.get("scan") or "")
        if rec.get("scan") and sp.exists():
            os.startfile(sp)
            return
        prov = rec.get("provenance") or {}
        png = R.render_source_page(p, prov.get("file", ""), prov.get("loc", ""))
        if png:
            tmp = project_paths(p)["out"] / f"скан_{key}.png"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(png)
            os.startfile(tmp)
        else:
            messagebox.showinfo("Скан", "Скан недоступен: исходник удалён после "
                                "индексации. Загрузите том и соберите показатели.")

    # ── ОТВЕТЫ ──
    def _tab_answers(self, f):
        row = ttk.Frame(f); row.pack(anchor="w", **PAD)
        ttk.Button(row, text="📄 Файл замечаний…",
                   command=self._pick_remarks).pack(side="left", padx=2)
        ttk.Button(row, text="① Найти ответы (фоном)",
                   command=self._start_answers).pack(side="left", padx=2)
        ttk.Button(row, text="⏹ Стоп",
                   command=lambda: self._stop_answers(False)).pack(side="left", padx=2)
        ttk.Button(row, text="Принять", command=lambda: self._decide("accepted")
                   ).pack(side="left", padx=(16, 2))
        ttk.Button(row, text="Отклонить", command=lambda: self._decide("rejected")
                   ).pack(side="left", padx=2)
        self.pb_ans = ttk.Progressbar(f, maximum=100)
        self.pb_ans.pack(fill="x", **PAD)
        self.lbl_ans = ttk.Label(f, text="—", wraplength=900, justify="left")
        self.lbl_ans.pack(anchor="w", **PAD)
        body = ttk.PanedWindow(f, orient="horizontal")
        body.pack(fill="both", expand=True, **PAD)
        self.tv_ans = ttk.Treeview(body, columns=("st",), show="tree headings")
        self.tv_ans.heading("#0", text="№ · замечание")
        self.tv_ans.heading("st", text="Статус")
        self.tv_ans.column("#0", width=430)
        self.tv_ans.column("st", width=100)
        self.tv_ans.bind("<<TreeviewSelect>>", lambda e: self._show_answer())
        body.add(self.tv_ans, weight=1)
        self.txt_ans = tk.Text(body, wrap="word", width=64)
        body.add(self.txt_ans, weight=2)
        self._refresh_answers()

    def _pick_remarks(self):
        p = filedialog.askopenfilename(
            title="Файл замечаний", filetypes=[("Документы", "*.docx *.pdf *.xlsx *.txt")])
        if not p:
            return
        import shutil
        rd = project_paths(self.project.get())["remarks_dir"]
        rd.mkdir(parents=True, exist_ok=True)
        dest = rd / Path(p).name
        shutil.copy2(p, dest)
        self._remarks_path = str(dest)
        self.status.config(text=f"Файл замечаний: {dest.name}")

    def _start_answers(self):
        from pmoos.pipeline.block1_answers import start_answers_background
        pid = start_answers_background(self.project.get(), object_type=self._ot(),
                                       remarks_path=getattr(self, "_remarks_path", None))
        self.status.config(text=f"Поиск ответов запущен (pid {pid})."
                           if pid else "Поиск ответов уже идёт.")

    def _stop_answers(self, hard: bool):
        from pmoos.pipeline.block1_answers import stop_answers
        stop_answers(self.project.get(), hard=hard)

    def _refresh_answers(self):
        from pmoos.pipeline.block1_answers import load_answers
        self.tv_ans.delete(*self.tv_ans.get_children())
        st_ru = {"proposed": "⚠ новый", "accepted": "принят",
                 "edited": "правка", "rejected": "отклонён"}
        for a in (load_answers(self.project.get()) or {}).get("answers", []):
            num = str(a.get("number", ""))
            self.tv_ans.insert("", "end", iid=num,
                               text=f"{num} · {(a.get('remark') or '')[:64]}",
                               values=(st_ru.get(a.get("status", ""), "?"),))

    def _show_answer(self):
        from pmoos.pipeline.block1_answers import load_answers
        sel = self.tv_ans.selection()
        if not sel:
            return
        num = sel[0]
        for a in (load_answers(self.project.get()) or {}).get("answers", []):
            if str(a.get("number")) == num:
                parts = [f"ЗАМЕЧАНИЕ №{num}:", a.get("remark", ""), "",
                         "ОТВЕТ:", a.get("answer", "")]
                if a.get("edit_location"):
                    parts += ["", f"ГДЕ ПРАВИТЬ: {a['edit_location']}"]
                if a.get("edit_was"):
                    parts += [f"БЫЛО: «{a['edit_was']}»"]
                if a.get("edit_shall"):
                    parts += [f"СТАЛО: «{a['edit_shall']}»"]
                if a.get("attachments"):
                    parts += ["ПРИЛОЖИТЬ: " + "; ".join(a["attachments"])]
                if a.get("missing_data"):
                    parts += [f"НЕ ХВАТАЕТ: {a['missing_data']}"]
                src = "\n".join(f"  [{s.get('n','')}] {s.get('file','')} {s.get('loc','')}"
                                for s in (a.get("sources") or [])[:6])
                if src:
                    parts += ["", "ИСТОЧНИКИ:", src]
                self.txt_ans.delete("1.0", "end")
                self.txt_ans.insert("1.0", "\n".join(str(x) for x in parts))
                break

    def _decide(self, status: str):
        sel = self.tv_ans.selection()
        if not sel:
            return
        from pmoos.pipeline.block1_answers import set_decision
        for num in sel:
            set_decision(self.project.get(), num, status=status)
        self._refresh_answers()

    # ── ВЫГРУЗКА ──
    def _tab_export(self, f):
        ttk.Label(f, text="Все файлы формируются в папку out проекта и открываются "
                          "сами.").pack(anchor="w", **PAD)
        def btn(text, job):
            def go():
                self.status.config(text=f"{text}…")
                def done(r):
                    if isinstance(r, Exception):
                        messagebox.showerror("Ошибка", str(r))
                        self.status.config(text="Ошибка.")
                    else:
                        self.status.config(text=f"Готово: {r}")
                        try:
                            os.startfile(r if isinstance(r, (str, Path))
                                         else project_paths(self.project.get())["out"])
                        except OSError:
                            pass
                _bg(job, done)
            ttk.Button(f, text=text, width=56, command=go).pack(anchor="w", padx=8, pady=3)
        p = self.project.get  # лениво: проект могут сменить
        btn("📋 Таблица ответов для экспертизы (docx + xlsx)",
            lambda: __import__("pmoos.output.answers_table", fromlist=["x"])
            .build_answers_table_docx(p()))
        btn("📋 Таблица изменений «что на что меняется» (xlsx)",
            lambda: __import__("pmoos.output.changes_table", fromlist=["x"])
            .build_changes_xlsx(p()))
        btn("📋 Ведомость недостающих данных (xlsx)",
            lambda: __import__("pmoos.output.gaps", fromlist=["x"])
            .build_gaps_xlsx(p()))
        btn("🏗 Каркас раздела из базы (docx, по выбранному разделу)",
            lambda: __import__("pmoos.output.section_draft", fromlist=["x"])
            .build_section_draft(p(), str(load_config().get("target_section", "OOS"))))
        ttk.Button(f, text="🗂 Открыть папку результатов", width=56,
                   command=lambda: os.startfile(project_paths(p())["out"])
                   ).pack(anchor="w", padx=8, pady=3)

    # ── УПРЗА ──
    def _tab_uprza(self, f):
        row = ttk.Frame(f); row.pack(anchor="w", **PAD)
        ttk.Button(row, text="📤 Сформировать выгрузку для УПРЗА",
                   command=self._uprza_out).pack(side="left", padx=2)
        ttk.Button(row, text="📥 Импортировать результаты из УПРЗА…",
                   command=self._uprza_in).pack(side="left", padx=2)
        self.tv_upr = ttk.Treeview(f, columns=("n", "v"), show="tree headings")
        self.tv_upr.heading("#0", text="Код ЗВ")
        self.tv_upr.heading("n", text="Вещество")
        self.tv_upr.heading("v", text="Макс, доли ПДК")
        self.tv_upr.column("#0", width=90)
        self.tv_upr.column("n", width=380)
        self.tv_upr.column("v", width=140)
        self.tv_upr.pack(fill="both", expand=True, **PAD)
        self._refresh_uprza()

    def _refresh_uprza(self):
        from pmoos.output.uprza_import import load_uprza_results
        self.tv_upr.delete(*self.tv_upr.get_children())
        r = load_uprza_results(self.project.get())
        for row in (r or {}).get("rows", [])[:60]:
            self.tv_upr.insert("", "end", text=row["code"],
                               values=(row["name"] or "—", f"{row['max_pdk']:g}"))

    def _uprza_out(self):
        def done(r):
            if isinstance(r, Exception):
                messagebox.showerror("Ошибка", str(r))
            else:
                self.status.config(text="Выгрузка для УПРЗА готова.")
                os.startfile(project_paths(self.project.get())["out"])
        _bg(lambda: __import__("pmoos.output.uprza_export", fromlist=["x"])
            .build_uprza_export(self.project.get()), done)

    def _uprza_in(self):
        p = filedialog.askopenfilename(title="Файл результатов УПРЗА",
                                       filetypes=[("Результаты", "*.txt *.csv *.xlsx")])
        if not p:
            return
        try:
            from pmoos.output.uprza_import import import_uprza_results
            r = import_uprza_results(self.project.get(), Path(p))
            self._refresh_uprza()
            exc = r.get("exceedances") or []
            msg = f"Импортировано веществ: {len(r['rows'])}."
            if exc:
                msg += "\n⚠ ПРЕВЫШЕНИЯ >1 ПДК: " + ", ".join(
                    f"{x['code']} — {x['max_pdk']:.2f}" for x in exc[:6])
            messagebox.showinfo("УПРЗА", msg)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Ошибка", str(e))

    # ── опрос фоновых процессов ──
    def _poll(self):
        try:
            from pmoos.index.indexer import read_state
            st = read_state(self.project.get())
            total, done = int(st.get("total_files") or 0), int(st.get("done_files") or 0)
            self.pb_idx["value"] = (done / total * 100) if total else 0
            self.lbl_idx.config(text=f"[{st.get('status', 'idle')}] "
                                     f"{done}/{total} · {st.get('message', '')[:220]}")
            from pmoos.pipeline.block1_answers import read_answers_state
            a = read_answers_state(self.project.get())
            t2, d2 = int(a.get("total") or 0), int(a.get("done") or 0)
            self.pb_ans["value"] = (d2 / t2 * 100) if t2 else 0
            _msg = f"[{a.get('status', 'idle')}] {d2}/{t2} · {a.get('message', '')[:220]}"
            if getattr(self, "_ans_last", "") != _msg:
                self._ans_last = _msg
                self.lbl_ans.config(text=_msg)
                if a.get("status") == "done":
                    self._refresh_answers()
            # найденные данные в БАЗЕ
            from pmoos.data import registry as R
            reg = R.load_registry(self.project.get())
            found = [(m, (reg.get("indicators") or {}).get(m["key"], {}))
                     for m in R.INDICATORS]
            cur = {i for i in self.tv_found.get_children()}
            for m, rec in found:
                if not str(rec.get("value", "")).strip():
                    continue
                prov = rec.get("provenance") or {}
                vals = (f"{rec.get('value')} {rec.get('unit', '')}",
                        f"{prov.get('file', '')} {prov.get('loc', '')}")
                if m["key"] in cur:
                    self.tv_found.item(m["key"], values=vals)
                else:
                    self.tv_found.insert("", "end", iid=m["key"],
                                         text=m["label"], values=vals)
        except Exception:  # noqa: BLE001 — опрос не должен ронять окно
            pass
        self.after(2000, self._poll)


def main():
    global _root
    _root = tk.Tk()
    _root.title(f"СТРОЙ.RAG — {VERSION}")
    _root.geometry("1180x760")
    try:
        from tkinter import font as tkfont
        for fname in ("TkDefaultFont", "TkTextFont", "TkHeadingFont"):
            tkfont.nametofont(fname).configure(size=11)
    except Exception:  # noqa: BLE001
        pass
    style = ttk.Style()
    if "vista" in style.theme_names():
        style.theme_use("vista")
    App(_root)
    # при старте — закрыть застрявшие сеансы и подтянуть ключи из облака
    def startup():
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
    threading.Thread(target=startup, daemon=True).start()
    _root.mainloop()


if __name__ == "__main__":
    main()
