@echo off
chcp 866 >nul
REM ============================================================
REM  STR.RAG: собрать нумерованный релиз в releases\STR.RAG-vX.Y.Z.zip
REM  (как в ЭКО.DOC). Имя файла = версия из кода — всегда совпадает.
REM ============================================================
cd /d "%~dp0"
set "PY=%PMOOS_DATA_DIR%\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%USERPROFILE%\.pmoos-rag\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" scripts\build_release.py
echo.
pause
