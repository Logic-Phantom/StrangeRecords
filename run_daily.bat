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

REM 절전 해제 직후에는 네트워크(DNS)가 늦게 붙어 Gemini 호출이 실패하므로 최대 10분 대기
set /a NET_WAIT=0
:wait_network
powershell -NoProfile -Command "try { [Net.Dns]::GetHostAddresses('generativelanguage.googleapis.com') | Out-Null; exit 0 } catch { exit 1 }" > nul 2>&1
if %ERRORLEVEL% equ 0 goto network_ready
set /a NET_WAIT+=1
if %NET_WAIT% geq 30 (
    echo [%date% %time%] network not ready after 10 min, continue anyway >> "logs\task_scheduler.log"
    goto network_ready
)
timeout /t 20 /nobreak > nul
goto wait_network
:network_ready

"%PY%" -m app.main today >> "logs\task_scheduler.log" 2>&1
set RC=%ERRORLEVEL%
echo [%date% %time%] run_daily end (exit %RC%) >> "logs\task_scheduler.log"
exit /b %RC%
