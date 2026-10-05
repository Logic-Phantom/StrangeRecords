@echo off
REM ============================================================
REM  기묘한 기록 - 매일 자동 실행 (Windows 작업 스케줄러에서 호출)
REM  이 파일이 있는 폴더(프로젝트 루트)를 기준으로 실행된다.
REM ============================================================
chcp 65001 > nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

if not exist "logs" mkdir logs
echo [%date% %time%] run_daily start >> "logs\task_scheduler.log"
"%PY%" -m app.main today >> "logs\task_scheduler.log" 2>&1
set RC=%ERRORLEVEL%
echo [%date% %time%] run_daily end (exit %RC%) >> "logs\task_scheduler.log"
exit /b %RC%
