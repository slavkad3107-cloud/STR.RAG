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
goto GO
:ACTLOCAL
call .venv\Scripts\activate.bat
:GO
echo Starting PMOOS-RAG ... a browser tab will open.
echo To stop: press Ctrl+C in this window.
streamlit run app\hub.py
pause
