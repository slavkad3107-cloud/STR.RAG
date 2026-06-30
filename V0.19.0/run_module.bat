@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PMOOS_DATA=%PMOOS_DATA_DIR%"
if "%PMOOS_DATA%"=="" set "PMOOS_DATA=%USERPROFILE%\.pmoos-rag"
if exist "%PMOOS_DATA%\venv\Scripts\activate.bat" goto MACTD
if exist .venv\Scripts\activate.bat goto MACTL
goto NOVENV
:MACTD
call "%PMOOS_DATA%\venv\Scripts\activate.bat"
goto MGO
:MACTL
call .venv\Scripts\activate.bat
:MGO

REM If arguments were passed - run them directly (advanced mode):
REM   run_module.bat modules\module1_inventory.py --project "Name" --uploads files
if not "%~1"=="" goto DIRECT

:MENU
echo ============================================================
echo   PMOOS-RAG - run a single module (CLI)
echo   For normal work use run.bat (the app). This is advanced.
echo ============================================================
echo   1 - Module 1: systematize project docs (inventory)
echo   2 - Module 2: build RAG index
echo   3 - Module 3: links graph + cascade + knowledge
echo   4 - Module 4: answers to remarks (list)
echo   5 - Module 5: export corrected OOS + answers table
echo   6 - Module 6: UPRZA export
echo   0 - exit
echo ------------------------------------------------------------
set "CH="
set /p "CH=Choose module (0-6): "
if "%CH%"=="0" goto END
if "%CH%"=="" goto MENU
set "PRJ="
set /p "PRJ=Project name (as in the app): "
if "%PRJ%"=="" goto MENU

if "%CH%"=="1" (
  set "UP="
  set /p "UP=Folder with files to load (Enter to skip): "
)

echo.
if "%CH%"=="1" if "%UP%"=="" python modules\module1_inventory.py --project "%PRJ%" --show
if "%CH%"=="1" if not "%UP%"=="" python modules\module1_inventory.py --project "%PRJ%" --uploads "%UP%"
if "%CH%"=="2" python modules\module2_index.py --project "%PRJ%" --background
if "%CH%"=="3" python modules\module3_graph.py --project "%PRJ%"
if "%CH%"=="4" python modules\module4_answers.py --project "%PRJ%" --list
if "%CH%"=="5" python modules\module5_export.py --project "%PRJ%"
if "%CH%"=="6" python modules\module6_uprza.py --project "%PRJ%"
echo.
pause
goto END

:DIRECT
python %*
pause
goto END

:NOVENV
echo [ERROR] Environment not found. Run install.bat first.
pause

:END
