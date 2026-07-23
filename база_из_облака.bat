@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
REM ============================================================
REM  STR.RAG: zagruzka bazy iz OneDrive (pered rabotoy).
REM  Tonkaya obertka nad pmoos.core.transfer.sync_in - vsya zaschita
REM  (manifest: nedokachannaya OneDrive-kopiya NE stavitsya; strahovochnaya
REM  kopiya + avtootkat; zamok bazy) realizovana TAM, kak v knopke Modulya 2.
REM  ETO ZAMENYAET lokalnuyu bazu oblachnoy - podtverdite nizhe.
REM ============================================================
cd /d "%~dp0"
set "PY=%PMOOS_DATA_DIR%\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%USERPROFILE%\.pmoos-rag\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo VNIMANIE: lokalnaya baza budet ZAMENENA oblachnoy kopiey.
echo Prilozhenie dolzhno byt ZAKRYTO.
echo Nazhmite lyubuyu klavishu dlya prodolzheniya ili zakroyte okno...
pause >nul
echo.
"%PY%" -c "import sys; from pmoos.core.transfer import sync_in, default_dest; ok,msg=sync_in(default_dest()); print(msg); sys.exit(0 if ok else 1)"
echo.
pause
