"""Общая настройка тестов: корень репозитория в sys.path, чтобы `import pmoos`
работал при запуске pytest из любой папки (в т.ч. двойным кликом run_tests.bat).

Плюс ИЗОЛЯЦИЯ КАТАЛОГА ДАННЫХ: каждый тест работает в своей временной папке,
поэтому прогон тестов физически не может испортить рабочие config.yaml, .env,
provider_health.json и базу пользователя.
"""
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path_factory, monkeypatch):
    """PMOOS_DATA_DIR → своя временная папка на каждый тест.
    Тест может переопределить её своим monkeypatch.setenv — это нормально."""
    d = tmp_path_factory.mktemp("pmoos-data")
    monkeypatch.setenv("PMOOS_DATA_DIR", str(d))
    yield d
