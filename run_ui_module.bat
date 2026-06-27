@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PMOOS_DATA=%PMOOS_DATA_DIR%"
if "%PMOOS_DATA%"=="" set "PMOOS_DATA=%USERPROFILE%\.pmoos-rag"
if exist "%PMOOS_DATA%\venv\Scripts\activate.bat" goto ACTD
if exist .venv\Scripts\activate.bat goto ACTL
goto NOVENV
:ACTD
call "%PMOOS_DATA%\venv\Scripts\activate.bat"
goto MENU
:ACTL
call .venv\Scripts\activate.bat
:MENU
echo ============================================================
echo   PMOOS-RAG - run a single MODULE as a Streamlit app
echo   For the full app use run.bat
echo ============================================================
echo   1 - Module 1: systematize project docs
echo   2 - Module 2: RAG index
echo   3 - Module 3: links graph + cascade
echo   4 - Module 4: answers to remarks
echo   5 - Module 5: corrected OOS export
echo   6 - Module 6: UPRZA export
echo   0 - exit
echo ------------------------------------------------------------
set "CH="
set /p "CH=Choose module (0-6): "
if "%CH%"=="0" goto END
if "%CH%"=="1" goto R1
if "%CH%"=="2" goto R2
if "%CH%"=="3" goto R3
if "%CH%"=="4" goto R4
if "%CH%"=="5" goto R5
if "%CH%"=="6" goto R6
goto MENU
:R1
streamlit run app\modules_ui\module1.py
goto RUNEND
:R2
streamlit run app\modules_ui\module2.py
goto RUNEND
:R3
streamlit run app\modules_ui\module3.py
goto RUNEND
:R4
streamlit run app\modules_ui\module4.py
goto RUNEND
:R5
streamlit run app\modules_ui\module5.py
goto RUNEND
:R6
streamlit run app\modules_ui\module6.py
goto RUNEND
:RUNEND
echo.
pause
goto END
:NOVENV
echo [ERROR] Environment not found. Run install.bat first.
pause
:END
