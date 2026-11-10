@echo off
REM Run the backtester from this checkout. Does not copy or unzip market data.
REM See run.ps1 for the QQQ and SPY filled-tree examples.
setlocal
cd /d "%~dp0"
python -m backtester %*
exit /b %ERRORLEVEL%
