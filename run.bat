@echo off
chcp 866 >nul
cd /d "%~dp0"
set "PMOOS_DATA=%PMOOS_DATA_DIR%"
if "%PMOOS_DATA%"=="" set "PMOOS_DATA=%USERPROFILE%\.pmoos-rag"
set "PYC=%PMOOS_DATA%\venv\Scripts\python.exe"
if exist "%PYC%" goto RUN
set "PYC=.venv\Scripts\python.exe"
if exist "%PYC%" goto RUN
echo [ERROR] Environment not found. Run install.bat first.
pause
exit /b 1
:RUN
rem -- napominanie pro install (BEZ pause: ne blokiruet zapusk) --
set "REQHASH="
for /f "skip=1 tokens=1" %%h in ('certutil -hashfile requirements.txt SHA256 2^>nul') do if not defined REQHASH set "REQHASH=%%h"
set "OLDHASH="
if exist "%PMOOS_DATA%\venv\requirements.sha256" set /p OLDHASH=<"%PMOOS_DATA%\venv\requirements.sha256"
if not "%REQHASH%"=="%OLDHASH%" echo [i] Sostav zavisimostey mog izmenitsya - esli chto-to ne rabotaet, zapustite install.bat.
rem -- server v OTDELNOM svyornutom okne; on SAM otkroet brauzer kogda
rem -- budet gotov. Eto okno bat srazu zakryvaetsya.
start "STROY.RAG" /min "%PYC%" app\gui\server.py
