@echo off
REM ============================================================
REM  기묘한 기록 - 테스트 실행 (YouTube 업로드 없이 영상까지 제작)
REM  사용법: run_test.bat            (Gemini 로 새 주제 제작)
REM          run_test.bat --offline  (API Key 없이 샘플 대본으로 제작)
REM ============================================================
chcp 65001 > nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

"%PY%" -m app.main doctor
"%PY%" -m app.main test %*
pause
