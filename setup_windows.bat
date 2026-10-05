@echo off
REM ============================================================
REM  기묘한 기록 - Windows 최초 설치
REM  필요: Python 3.11+ (https://www.python.org/downloads/ 설치 시 "Add to PATH" 체크)
REM  FFmpeg 는 따로 설치하지 않아도 imageio-ffmpeg 내장 바이너리를 사용한다.
REM ============================================================
chcp 65001 > nul
setlocal
cd /d "%~dp0"

py -3.11 --version > nul 2>&1
if %ERRORLEVEL%==0 (
    set "BASEPY=py -3.11"
) else (
    set "BASEPY=python"
)

if not exist ".venv" (
    echo [1/3] 가상환경 생성
    %BASEPY% -m venv .venv || goto :error
)

echo [2/3] 패키지 설치
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :error

echo [3/3] 폴더/DB/기본 BGM·효과음 생성
".venv\Scripts\python.exe" -m app.main setup || goto :error
".venv\Scripts\python.exe" -m app.main doctor

echo.
echo 다음 단계:
echo   1) .env 파일에 GEMINI_API_KEY 등 입력
echo   2) run_test.bat --offline  (샘플 영상 제작 확인)
echo   3) run_test.bat            (Gemini 로 실제 제작 확인)
echo   4) .venv\Scripts\python.exe -m app.main auth   (YouTube 최초 인증)
echo   5) powershell -ExecutionPolicy Bypass -File scripts\register_windows_task.ps1
pause
exit /b 0

:error
echo 설치 실패
pause
exit /b 1
