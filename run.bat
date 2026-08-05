@echo off
chcp 866 >nul
cd /d "%~dp0"
set "PMOOS_DATA=%PMOOS_DATA_DIR%"
if "%PMOOS_DATA%"=="" set "PMOOS_DATA=%USERPROFILE%\.pmoos-rag"
if exist "%PMOOS_DATA%\venv\Scripts\python.exe" goto GDATA
if exist .venv\Scripts\python.exe goto GLOCAL
echo [ERROR] Environment not found. Run install.bat first.
pause
exit /b 1
:GDATA
rem -- predupredit, esli posle obnovleniya izmenilis zavisimosti --
set "REQHASH="
for /f "skip=1 tokens=1" %%h in ('certutil -hashfile requirements.txt SHA256 2^>nul') do if not defined REQHASH set "REQHASH=%%h"
if not exist "%PMOOS_DATA%\venv\requirements.sha256" goto GRUN
set /p OLDHASH=<"%PMOOS_DATA%\venv\requirements.sha256"
if "%REQHASH%"=="%OLDHASH%" goto GRUN
echo ============================================================
echo   VNIMANIE: sostav zavisimostey izmenilsya - nuzhen install.bat
echo   (odin raz posle obnovleniya; potom eto soobshenie ischeznet)
echo ============================================================
pause
:GRUN
"%PMOOS_DATA%\venv\Scripts\python.exe" app\gui\server.py
goto END
:GLOCAL
.venv\Scripts\python.exe app\gui\server.py
:END
