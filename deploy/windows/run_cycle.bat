@echo off
rem Один цикл парсинга — эту команду вызывает Планировщик задач Windows.
cd /d "%~dp0..\.."
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" run_cycle_once.py >> "System\logs\cron.log" 2>&1
) else (
    python run_cycle_once.py >> "System\logs\cron.log" 2>&1
)
exit /b %ERRORLEVEL%
