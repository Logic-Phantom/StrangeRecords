"""보조 Python 스케줄러.

기본 자동 실행은 Windows 작업 스케줄러(run_daily.bat)를 사용한다.
작업 스케줄러를 쓸 수 없는 환경에서 `python -m app.main schedule` 로 실행하면
매일 generate_time 에 today 파이프라인을 실행한다 (프로세스가 켜져 있어야 함).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from app.config.settings import Settings
from app.utils.logger import get_logger

logger = get_logger("scheduler")


def next_run_time(settings: Settings, now: datetime | None = None) -> datetime:
    tz = ZoneInfo(settings.schedule.timezone)
    now = now or datetime.now(tz)
    hour, minute = (int(x) for x in settings.schedule.generate_time.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def run_forever(settings: Settings, job: Callable[[], None], poll_seconds: int = 30) -> None:
    if not settings.schedule.enabled:
        logger.warning("schedule.enabled=false → 스케줄러를 실행하지 않습니다")
        return
    tz = ZoneInfo(settings.schedule.timezone)
    while True:
        target = next_run_time(settings)
        logger.info("다음 실행: %s", target.isoformat(timespec="minutes"))
        while datetime.now(tz) < target:
            time.sleep(min(poll_seconds, max(1, (target - datetime.now(tz)).total_seconds())))
        try:
            job()
        except Exception as exc:  # 스케줄러는 계속 살아 있어야 한다
            logger.error("예약 실행 실패: %s", exc)
