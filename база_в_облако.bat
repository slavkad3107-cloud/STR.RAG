@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
REM ============================================================
REM  STR.RAG: vygruzka bazy v OneDrive (posle raboty).
REM  Tonkaya obertka nad pmoos.core.transfer.sync_out - vsya zaschita
REM  (zamok bazy, manifest tselostnosti, otkaz pri zhivoy indeksatsii)
REM  realizovana TAM, chtoby povedenie sovpadalo s knopkoy v Module 2.
REM ============================================================
cd /d "%~dp0"
set "PY=%PMOOS_DATA_DIR%\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%USERPROFILE%\.pmoos-rag\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo Prilozhenie luchshe zakryt (Ctrl+C v okne run.bat).
echo.
"%PY%" -c "import sys; from pmoos.core.transfer import sync_out, default_dest; ok,msg=sync_out(default_dest()); print(msg); sys.exit(0 if ok else 1)"
echo.
pause
