@echo off
REM auto_push.bat - Run auto-push script without command line
REM Just double-click this file to start auto-pushing!

cd /d "%~dp0"
python auto_push_github.py --auto
pause
