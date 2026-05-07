@echo off
REM master_startup.bat - Start all services at once
REM Run this every morning to start the full trading bot pipeline

setlocal enabledelayedexpansion

echo.
echo ================================================
echo SCURO TRADING BOT - MASTER STARTUP
echo ================================================
echo.

cd /d "%~dp0"

REM Check Python
python --version > nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python not installed
    echo Install from: https://www.python.org
    pause
    exit /b 1
)

echo.
echo Starting services...
echo.

REM Terminal 1: MT5 History Data Feed
echo [1/3] Starting MT5 data feed (mt5_history.py)
echo Gathering live market data from MT5 terminal...
start "MT5 Data Feed" cmd /k python mt5_history.py

REM Wait a moment for mt5_history to start
timeout /t 3 /nobreak

REM Terminal 2: Supabase Uploader
echo [2/3] Starting Supabase uploader (upload_to_supabase.py)
echo Syncing data to cloud...
start "Supabase Uploader" cmd /k python upload_to_supabase.py

REM Wait a moment for uploader to start
timeout /t 3 /nobreak

REM Terminal 3: Auto-Push GitHub
echo [3/3] Starting auto-push (auto_push_github.py)
echo Pushing changes to GitHub every 5 minutes...
start "GitHub Auto-Push" cmd /k python auto_push_github.py --auto

echo.
echo ================================================
echo ALL SERVICES STARTED!
echo ================================================
echo.
echo Running Services:
echo  [1] MT5 Data Feed       - Generating scuro_live_data.json
echo  [2] Supabase Uploader   - Syncing to cloud
echo  [3] GitHub Auto-Push    - Backing up to GitHub
echo.
echo Your Dashboard:
echo  Dashboard URL: https://share.streamlit.io/USERNAME/sir_bane_v1/main/dashboard_cloud.py
echo  (Update USERNAME in Streamlit Cloud)
echo.
echo GitHub Repo:
echo  https://github.com/USERNAME/sir_bane_v1
echo.
echo To stop all services: Close all three windows above
echo.
echo Keep this window open (it can be minimized)
echo Press any key to close this launcher...
pause > nul
