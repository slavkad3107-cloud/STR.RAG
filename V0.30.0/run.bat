@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PMOOS_DATA=%PMOOS_DATA_DIR%"
if "%PMOOS_DATA%"=="" set "PMOOS_DATA=%USERPROFILE%\.pmoos-rag"
if exist "%PMOOS_DATA%\venv\Scripts\activate.bat" goto ACTDATA
if exist .venv\Scripts\activate.bat goto ACTLOCAL
echo [ERROR] Environment not found. Run install.bat first.
pause
exit /b 1
:ACTDATA
call "%PMOOS_DATA%\venv\Scripts\activate.bat"
set "VENVDIR=%PMOOS_DATA%\venv"
goto GO
:ACTLOCAL
call .venv\Scripts\activate.bat
set "VENVDIR=.venv"
:GO
REM ---- Предупреждение, если после обновления кода изменились зависимости ----
REM install.bat пишет хэш requirements.txt в venv; расхождение = нужен install.
if not exist requirements.txt goto REQOK
set "REQHASH="
for /f "skip=1 tokens=1" %%h in ('certutil -hashfile requirements.txt SHA256 2^>nul') do if not defined REQHASH set "REQHASH=%%h"
if not defined REQHASH goto REQOK
if exist "%VENVDIR%\requirements.sha256" goto REQCMP
REM venv установлен до появления этой проверки: принимаем текущий состав за базовый
>"%VENVDIR%\requirements.sha256" echo %REQHASH%
goto REQOK
:REQCMP
set "REQOLD="
set /p REQOLD=<"%VENVDIR%\requirements.sha256"
if /i "%REQHASH%"=="%REQOLD%" goto REQOK
echo.
echo ============================================================
echo   ВНИМАНИЕ: обновление изменило состав зависимостей
echo   (requirements.txt). Нужно ОДИН раз запустить install.bat,
echo   иначе приложение может не запуститься или сбоить.
echo ============================================================
echo Нажмите любую клавишу, чтобы всё равно попробовать запустить...
pause >nul
:REQOK
echo Starting STR.RAG ... a browser tab will open.
echo To stop: press Ctrl+C in this window.
streamlit run app\hub.py
pause
