"""Управление путями.

Ключевое требование пользователя:
  «База данных RAG должна находиться ОТДЕЛЬНО от приложения, чтобы не
   индексироваться заново».
  «Не сохранять сами проекты … только название и базу данных с чанками и токенами».

Поэтому всё, что должно ПЕРЕЖИВАТЬ переустановку/обновление приложения
(векторная база, кэш эмбеддингов, графы, решения, реестр проектов), хранится
в отдельном каталоге данных ВНЕ папки с кодом.

Каталог данных определяется так (по приоритету):
  1. переменная окружения PMOOS_DATA_DIR
  2. ~/.pmoos-rag  (домашняя папка пользователя)

Внутри каталога данных:
  data_dir/
    projects.json            — реестр проектов (только метаданные, без самих файлов ПД)
    qdrant/                  — векторная база Qdrant (embedded-режим)
    emb_cache/               — кэш эмбеддингов (sqlite)
    models/                  — локальный кэш скачанных моделей (HF)
    projects/<slug>/
        inventory.json       — карта разделов проекта
        contacts.json        — контакты проектировщиков/экспертов
        versions.json        — версии документов
        graph.json           — граф связей разделов
        decisions.jsonl      — принятые пользователем решения (human-in-the-loop)
        answers.json         — найденные ответы на замечания
        index_state.json     — состояние индексации (прогресс/пауза/возобновление)
        tmp_uploads/         — ВРЕМЕННЫЕ файлы ПД (удаляются после индексации)
        remarks/             — файлы замечаний для М4 (ПОСТОЯННЫЕ, очисткой не трогаются)
        out/                 — сформированные DOCX/XLSX/экспорт УПРЗА
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# Корень приложения (папка с кодом). Используется только для чтения шаблонов.
APP_ROOT = Path(__file__).resolve().parent.parent


def data_root() -> Path:
    """Корневой каталог данных (вне приложения)."""
    env = os.environ.get("PMOOS_DATA_DIR", "").strip()
    root = Path(env).expanduser() if env else (Path.home() / ".pmoos-rag")
    root.mkdir(parents=True, exist_ok=True)
    return root


def slugify(name: str) -> str:
    """Безопасное имя каталога из названия проекта (сохраняет кириллицу читаемой
    через транслит не делаем — просто чистим запрещённые символы)."""
    name = (name or "project").strip()
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    name = re.sub(r"\s+", "_", name)
    return name[:120] or "project"


def project_dir(project: str) -> Path:
    d = data_root() / "projects" / slugify(project)
    d.mkdir(parents=True, exist_ok=True)
    return d


def project_paths(project: str) -> dict[str, Path]:
    d = project_dir(project)
    return {
        "root": d,
        "inventory": d / "inventory.json",
        "contacts": d / "contacts.json",
        "versions": d / "versions.json",
        "graph": d / "graph.json",
        "decisions": d / "decisions.jsonl",
        "answers": d / "answers.json",
        "index_state": d / "index_state.json",
        "uploads": d / "tmp_uploads",   # временные файлы ПД
        "remarks_dir": d / "remarks",   # ПОСТОЯННАЯ папка файлов замечаний (М4)
        "out": d / "out",
    }


def qdrant_dir() -> Path:
    d = data_root() / "qdrant"
    d.mkdir(parents=True, exist_ok=True)
    return d


def emb_cache_path() -> Path:
    d = data_root() / "emb_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / "embeddings.sqlite"


def models_dir() -> Path:
    d = data_root() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def projects_registry() -> Path:
    return data_root() / "projects.json"
