"""제한된 횟수의 재시도 (무한 재시도 금지).

기본 정책: 1차 실패 → 5~10초 대기 → 2차 → 15~30초 대기 → 3차 → 실패 기록
"""

from __future__ import annotations

import random
import time
from typing import Callable, Iterable, Sequence, TypeVar

from app.utils.logger import get_logger, log_step_error

T = TypeVar("T")

DEFAULT_DELAYS: tuple[tuple[float, float], ...] = ((5, 10), (15, 30))

logger = get_logger("retry")


class NonRetryableError(Exception):
    """재시도해도 결과가 같은 오류 (설정 누락, 인증 정보 없음 등)."""


class RetryError(Exception):
    def __init__(self, step: str, attempts: int, last_error: BaseException):
        super().__init__(f"{step} 실패 ({attempts}회 시도): {type(last_error).__name__}: {last_error}")
        self.step = step
        self.attempts = attempts
        self.last_error = last_error


def _delay_for(attempt: int, delays: Sequence[Sequence[float]]) -> float:
    if not delays:
        return 0.0
    low, high = delays[min(attempt - 1, len(delays) - 1)]
    return random.uniform(low, high)


def retry_call(
    func: Callable[[], T],
    *,
    step: str,
    max_attempts: int = 3,
    delays: Sequence[Sequence[float]] = DEFAULT_DELAYS,
    retry_on: Iterable[type[BaseException]] = (Exception,),
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    retry_on = tuple(retry_on)
    last_error: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return func()
        except NonRetryableError:
            raise
        except retry_on as exc:  # type: ignore[misc]
            last_error = exc
            log_step_error(logger, step, exc, attempt, max_attempts)
            if attempt >= max_attempts:
                break
            wait = _delay_for(attempt, delays)
            logger.info("%s 재시도 대기 %.1f초 (%d/%d)", step, wait, attempt + 1, max_attempts)
            sleep(wait)
    assert last_error is not None
    raise RetryError(step, max_attempts, last_error) from last_error
