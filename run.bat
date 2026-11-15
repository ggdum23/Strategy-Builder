@echo off
setlocal
cd /d "%~dp0"
python -m backtester %*
exit /b %ERRORLEVEL%
