#!/usr/bin/env bash
# 기묘한 기록 - 매일 자동 실행 (macOS/Linux: launchd 또는 cron 에서 호출)
set -u
cd "$(dirname "$0")"
PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"
mkdir -p logs
echo "[$(date '+%F %T')] run_daily start" >> logs/task_scheduler.log
# 이전 영상 성과 수집 (보조 기능: 실패해도 제작은 계속)
"$PY" -m app.main analytics sync >> logs/task_scheduler.log 2>&1 || true
"$PY" -m app.main today >> logs/task_scheduler.log 2>&1
RC=$?
echo "[$(date '+%F %T')] run_daily end (exit $RC)" >> logs/task_scheduler.log
exit $RC
