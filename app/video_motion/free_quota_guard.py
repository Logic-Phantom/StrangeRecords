"""무료 사용량 안에서만 Image-to-Video 요청을 보내도록 막는 가드.

모든 외부 요청은 보내기 전에 allow() 를 통과해야 한다. 막히는 경우:
- 이번 작업에서 이미 중단됨 (무료 한도/크레딧/결제 오류, quota_stop_fallback)
- provider 가 공식 조사 기록상 verified_free 가 아님 (unverified/paid → 설정으로 우회 불가)
- provider 가 이번 작업에서 비활성 (인증 오류/모델 거부/일시 오류 반복)
- 작업당 / 하루 요청 수, 영상 길이 한도 초과 (공식 무료 한도를 알면 그보다 낮은 값을 사용)

free_only=false, paid_fallback_enabled=true 로 설정해도 여기서 강제로 무시한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config.settings import VideoMotionConfig
from app.utils.logger import get_logger
from app.video_motion.base import FreeStatus, ImageToVideoProvider

logger = get_logger("motion_guard")


@dataclass
class GuardDecision:
    allowed: bool
    reason: str = ""


@dataclass
class FreeQuotaGuard:
    config: VideoMotionConfig
    requests_today: int = 0                # 오늘(KST) 이미 보낸 실제 요청 수 (DB 기록, 다른 작업 포함)
    job_requests_before: int = 0           # 이 작업이 이전 실행(재시도 전)에서 이미 보낸 요청 수
    requests: int = 0                      # 이번 실행에서 보낸 요청 수
    seconds_requested: float = 0.0
    stopped: bool = False
    stop_reason: str = ""
    quota_limited: bool = False
    disabled: dict[str, str] = field(default_factory=dict)
    consecutive_failures: dict[str, int] = field(default_factory=dict)
    blocked_after_stop: int = 0            # 중단 이후 들어온 요청 시도 (실제로 보내지 않음, 검사용)

    def __post_init__(self) -> None:
        # 비용 안전 정책: 설정 실수로 바뀌어도 무료 전용 + 유료 fallback 금지
        if not self.config.free_only:
            logger.warning("video_motion.free_only=false 는 허용되지 않습니다 → 무료 전용으로 강제")
        if self.config.paid_fallback_enabled:
            logger.warning("video_motion.paid_fallback_enabled=true 는 허용되지 않습니다 → 유료 fallback 금지로 강제")
        self.free_only = True
        self.paid_fallback_enabled = False

    # ------------------------------------------------------------ limits
    def job_limit(self, provider: ImageToVideoProvider | None = None) -> int:
        return max(int(self.config.max_requests_per_job), 0)

    def day_limit(self, provider: ImageToVideoProvider | None = None) -> int:
        limit = max(int(self.config.max_requests_per_day), 0)
        official = getattr(provider, "official_free_requests_per_day", None) if provider else None
        if official is not None:
            # 공식 무료 한도보다 낮은 안전 한도 (다른 용도로 쓴 요청이 있을 수 있으므로 절반)
            limit = min(limit, max(int(official) // 2, 0))
        return limit

    def clip_limit(self, provider: ImageToVideoProvider | None = None) -> float:
        limit = float(self.config.max_clip_seconds)
        own = getattr(provider, "max_clip_seconds", None) if provider else None
        return min(limit, float(own)) if own else limit

    @property
    def job_requests(self) -> int:
        return self.job_requests_before + self.requests

    # ------------------------------------------------------------ checks
    def provider_allowed(self, provider: ImageToVideoProvider) -> GuardDecision:
        if provider.free_status == FreeStatus.PAID:
            return GuardDecision(False, f"{provider.name}: 유료 전용 provider (호출 금지)")
        if provider.free_status != FreeStatus.VERIFIED_FREE:
            return GuardDecision(False, f"{provider.name}: 무료 사용 조건 미검증 ({provider.free_status}) → 호출하지 않음")
        if provider.name in self.disabled:
            return GuardDecision(False, f"{provider.name}: 이번 작업에서 비활성 ({self.disabled[provider.name]})")
        if not provider.configured:
            return GuardDecision(False, f"{provider.name}: 키/모델 설정 없음")
        return GuardDecision(True)

    def allow(self, provider: ImageToVideoProvider, clip_seconds: float) -> GuardDecision:
        if self.stopped:
            self.blocked_after_stop += 1
            return GuardDecision(False, f"외부 영상 생성 중단됨: {self.stop_reason}")
        decision = self.provider_allowed(provider)
        if not decision.allowed:
            return decision
        if self.job_requests >= self.job_limit(provider):
            return GuardDecision(False, f"작업당 요청 한도 도달 ({self.job_requests}/{self.job_limit(provider)})")
        if self.requests_today + self.requests >= self.day_limit(provider):
            return GuardDecision(False, f"하루 요청 한도 도달 ({self.requests_today + self.requests}/{self.day_limit(provider)})")
        if clip_seconds > self.clip_limit(provider) + 1e-6:
            return GuardDecision(False, f"요청 길이 {clip_seconds:.1f}s > 한도 {self.clip_limit(provider):.1f}s")
        if self.seconds_requested + clip_seconds > float(self.config.max_total_video_seconds_per_job) + 1e-6:
            return GuardDecision(False, f"작업당 영상 길이 한도 도달 ({self.seconds_requested:.1f}s)")
        return GuardDecision(True)

    # ------------------------------------------------------------ events
    def record_request(self, clip_seconds: float) -> None:
        """실제로 요청을 보낸 직후 호출 (실패한 요청도 무료 사용량을 쓸 수 있으므로 센다)."""
        self.requests += 1
        self.seconds_requested += clip_seconds

    def record_success(self, provider: ImageToVideoProvider) -> None:
        self.consecutive_failures.pop(provider.name, None)

    def record_transient_failure(self, provider: ImageToVideoProvider) -> bool:
        """일시 오류 누적. 연속 한도에 도달하면 provider 를 비활성화하고 True."""
        count = self.consecutive_failures.get(provider.name, 0) + 1
        self.consecutive_failures[provider.name] = count
        if count >= max(int(self.config.max_consecutive_transient_failures), 1):
            self.disable(provider, f"일시 오류 {count}회 연속")
            return True
        return False

    def disable(self, provider: ImageToVideoProvider, reason: str) -> None:
        if provider.name not in self.disabled:
            self.disabled[provider.name] = reason
            logger.warning("Image-to-Video %s 이번 작업에서 비활성: %s", provider.name, reason)

    def stop_for_cost(self, provider: ImageToVideoProvider, reason: str) -> None:
        """무료 한도/크레딧/결제 오류. 기본(quota_stop_fallback)은 이번 작업의 외부 호출 전체 중단."""
        self.quota_limited = True
        self.disable(provider, reason)
        if self.config.quota_stop_fallback:
            self.stop(f"{provider.name}: {reason}")

    def stop(self, reason: str) -> None:
        if not self.stopped:
            self.stopped = True
            self.stop_reason = reason
            logger.warning("Image-to-Video 외부 호출 중단 → 남은 장면은 FFmpeg 움직임 효과: %s", reason)
