@echo off
chcp 866 >nul
cd /d "%~dp0"
set "PMOOS_DATA=%PMOOS_DATA_DIR%"
if "%PMOOS_DATA%"=="" set "PMOOS_DATA=%USERPROFILE%\.pmoos-rag"
if exist "%PMOOS_DATA%env\Scripts\pythonw.exe" goto GDATA
if exist .venv\Scripts\pythonw.exe goto GLOCAL
echo [ERROR] Environment not found. Run install.bat first.
pause
exit /b 1
:GDATA
start "" "%PMOOS_DATA%env\Scripts\pythonw.exe" app
ative.py
goto END
:GLOCAL
start "" .venv\Scripts\pythonw.exe app
ative.py
:END
