@echo off
REM HireAssist daily pipeline runner
REM Called by Windows Task Scheduler or run manually

cd /d "%~dp0"

REM Create log directory if needed
if not exist "data\logs" mkdir "data\logs"

set LOGFILE=data\logs\pipeline.log

echo. >> "%LOGFILE%"
echo ====================================================== >> "%LOGFILE%"
echo [%date% %time%] Pipeline starting >> "%LOGFILE%"
echo ====================================================== >> "%LOGFILE%"

python daily_intelligence.py >> "%LOGFILE%" 2>&1

echo [%date% %time%] Pipeline finished (exit code: %ERRORLEVEL%) >> "%LOGFILE%"
