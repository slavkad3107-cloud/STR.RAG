@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM ---- Robust against slow / unreliable internet (pythonhosted read timeouts) ----
set PIP_DEFAULT_TIMEOUT=120
echo ============================================================
echo   PMOOS-RAG v0.17.0 Faster - install (Windows)
echo ============================================================

where python >nul 2>nul
if errorlevel 1 goto NOPY

echo.
echo [1/5] Shared environment in the DATA folder ...
set "PMOOS_DATA=%PMOOS_DATA_DIR%"
if "%PMOOS_DATA%"=="" set "PMOOS_DATA=%USERPROFILE%\.pmoos-rag"
if not exist "%PMOOS_DATA%" mkdir "%PMOOS_DATA%"
set "PIP_CACHE_DIR=%PMOOS_DATA%\pip-cache"
set "VENV=%PMOOS_DATA%\venv"
echo     venv:      %VENV%
echo     pip cache: %PIP_CACHE_DIR%
echo     New app versions REUSE this - nothing is downloaded twice.
if exist "%VENV%\Scripts\activate.bat" goto VENVOK
python -m venv "%VENV%"
:VENVOK
call "%VENV%\Scripts\activate.bat"

echo.
echo [2/5] Upgrading pip ...
python -m pip install --upgrade pip --timeout 30 --retries 2

echo.
echo [3/5] Installing PyTorch with CUDA 12.4 ...
echo     For NVIDIA GPU such as RTX 3070 Ti. No GPU - see README, section GPU/CPU.
pip install torch==2.6.0 --index-url https://download.pytorch.org/whl/cu124 --timeout 180 --retries 10
if not errorlevel 1 goto TORCHOK
echo WARNING: GPU torch failed. Installing CPU build instead ...
pip install torch==2.6.0 --timeout 180 --retries 10
:TORCHOK

echo.
echo [4/5] Installing dependencies (NumPy + the rest) ...
echo     Attempt 1: default index, fail-fast ...
pip install --prefer-binary --timeout 60 --retries 2 -r requirements.txt
if not errorlevel 1 goto DEPSOK

echo.
echo     Default index (pythonhosted) is unreliable on this network.
echo     Switching to a reliable mirror for ALL remaining downloads ...
set PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
set PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

echo     Attempt 2: same packages from the mirror ...
pip install --prefer-binary --timeout 120 --retries 10 -r requirements.txt
if not errorlevel 1 goto DEPSOK

echo.
echo     Attempt 3: installing packages ONE BY ONE from the mirror (kept on success) ...
for /f "usebackq eol=# tokens=1 delims= " %%P in ("requirements.txt") do (
  echo        package: %%P
  pip install --prefer-binary --timeout 120 --retries 10 "%%P"
)
:DEPSOK

echo.
echo [5/5] Downloading AI models (bge-m3 + reranker, ~3.4 GB) and SELF-TEST ...
echo     First install may take 10-30 minutes depending on the network.
echo     Re-run resumes the download; progress bars are shown below.
python setup_models.py
if errorlevel 1 (
  echo.
  echo [WARNING] Model download or self-test failed. The app will still start.
  echo Retry later with:   python setup_models.py
  echo Or use the button "Download all models now" in Module 2.
)

echo.
echo Verifying NumPy + GPU ...
python -c "import numpy; print('NumPy:', numpy.__version__)"
python -c "import torch; print('CUDA available:', torch.cuda.is_available())"

echo.
echo.
echo Setting up the API keys file in the DATA folder ...
copy /y .env.example "%PMOOS_DATA%\.env.example" >nul
if exist "%PMOOS_DATA%\.env" goto ENVOK
if not exist .env goto ENVNEW
copy .env "%PMOOS_DATA%\.env" >nul
echo     migrated legacy .env from the app folder
goto ENVOK
:ENVNEW
copy .env.example "%PMOOS_DATA%\.env" >nul
echo     created from .env.example
:ENVOK
echo     keys file: %PMOOS_DATA%\.env

echo ============================================================
echo   If a package was still missed, just run install.bat again
echo   (it resumes from cache / mirror).
echo   Start the UI:      run.bat
echo   Run one module:    run_module.bat modules\module1_inventory.py --project "Name"
echo ------------------------------------------------------------
echo   API keys file: %PMOOS_DATA%\.env  (or set keys in the app sidebar)
echo   Old ".venv" folders inside previous app copies can be deleted.
echo ============================================================
pause
goto END

:NOPY
echo [ERROR] Python not found.
echo Install Python 3.10-3.12 from https://python.org
echo and enable "Add Python to PATH" during setup, then re-run install.bat
pause

:END
