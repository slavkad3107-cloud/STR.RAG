@echo off
REM Запуск юнит-тестов чистой логики СтройПроекта (двойной клик).
REM Тесты быстрые и не грузят нейросети; нужен pytest (pip install pytest).
setlocal
cd /d "%~dp0"
echo === СтройПроект: юнит-тесты чистой логики ===
python -m pytest tests -q
if errorlevel 1 (
  echo.
  echo [!] Есть падения. Если ошибка «No module named pytest» — выполните:
  echo     python -m pip install pytest
) else (
  echo.
  echo [OK] Все тесты пройдены.
)
echo.
pause
