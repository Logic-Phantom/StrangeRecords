#!/usr/bin/env bash
# ============================================================
#  기묘한 기록 - macOS 최초 설치
#  Python 3.11+ 가 없으면 uv(https://docs.astral.sh/uv/)로 설치한다.
#  FFmpeg 는 imageio-ffmpeg 내장 바이너리를 사용하므로 별도 설치 불필요.
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  if command -v python3.11 >/dev/null 2>&1; then
    python3.11 -m venv .venv
  else
    command -v uv >/dev/null 2>&1 || python3 -m pip install --user uv
    UV="$(command -v uv || echo "$HOME/Library/Python/$(python3 -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")')/bin/uv")"
    "$UV" python install 3.11
    "$UV" venv --python 3.11 .venv
    "$UV" pip install --python .venv/bin/python pip
  fi
fi

.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m app.main setup
chmod +x run_daily.sh scripts/*.sh
.venv/bin/python -m app.main doctor || true
echo
echo "다음: .env 에 API Key 입력 → .venv/bin/python -m app.main test --offline"
