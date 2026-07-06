"""Общая настройка тестов: корень репозитория в sys.path, чтобы `import pmoos`
работал при запуске pytest из любой папки (в т.ч. двойным кликом run_tests.bat)."""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
