"""Image-to-Video Provider 공통 인터페이스 + 오류 분류.

- Provider 는 기존 AI 정지 이미지 1장 + 움직임 프롬프트로 짧은 영상 bytes 를 만든다.
- 무료 여부는 설정값이 아니라 각 Provider 클래스의 `free_status`(공식 문서 조사 기록)로만 판단한다.
  verified_free 가 아니면 FreeQuotaGuard 가 요청을 막는다 (설정으로 우회 불가).
- 오류는 MotionProviderError(kind=...) 로 분류해 올린다. 서비스는 kind 에 따라 중단/비활성/제한된 재시도를 정한다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


class FreeStatus:
    VERIFIED_FREE = "verified_free"   # 공식 문서로 무료 사용량 + 초과 시 과금 없이 거절됨을 확인
    UNVERIFIED = "unverified"         # 무료 여부/초과 시 과금 차단을 확인하지 못함 → 호출 금지
    PAID = "paid"                     # 유료 전용 → 어떤 설정으로도 호출 금지


class ErrorKind:
    QUOTA = "quota_exceeded"          # 무료 요청 한도/할당량 소진 (429 등)
    BILLING = "billing_required"      # 402, 크레딧 소진, 결제 필요
    AUTH = "auth_error"               # 401/403 (무료 한도 문제로 단정하지 않음)
    RATE_LIMIT = "rate_limited"       # 일시적인 속도 제한 (명시적인 경우만)
    TRANSIENT = "transient"           # 5xx, 타임아웃, 네트워크
    INVALID_REQUEST = "invalid_request"  # 400/404/422: 모델 없음/파라미터 거부
    INVALID_RESULT = "invalid_result"    # 응답이 정상 영상이 아님
    UNKNOWN = "unknown"

    STOP_ALL = (QUOTA, BILLING)                 # 비용 관련 → 추가 요청 없이 중단
    DISABLE_PROVIDER = (AUTH, INVALID_REQUEST)  # 이 provider 만 이번 작업에서 비활성
    RETRYABLE = (RATE_LIMIT, TRANSIENT)         # 제한된 재시도 허용


_QUOTA_WORDS = (
    "quota", "exceeded", "limit reached", "daily limit", "monthly", "neurons", "free tier", "free-tier",
    "allocation", "usage limit", "resource_exhausted", "resource exhausted", "out of credits",
    "额度", "上限", "次数",
)
_BILLING_WORDS = (
    "credit", "billing", "payment", "pay-as-you-go", "insufficient", "balance", "upgrade", "subscribe",
    "purchase", "pro plan", "paid plan", "prepaid", "余额", "欠费", "充值", "付费",
)
_RATE_WORDS = ("rate limit", "rate-limit", "ratelimit", "too many requests", "slow down", "requests per minute",
               "per second", "concurrent", "频率", "速率", "并发")


def classify_http_error(status: int | None, body: str = "", retry_after: str | None = None) -> str:
    """HTTP 상태 + 응답 본문으로 오류 종류를 정한다.

    429 는 본문으로 구분한다: 할당량/크레딧 단어가 있으면 QUOTA, 속도 제한 단어(또는 Retry-After)만 있으면 RATE_LIMIT,
    어느 쪽인지 알 수 없으면 비용 안전을 위해 QUOTA 로 본다 (추가 요청 없이 중단).
    """
    text = (body or "").lower()
    has_billing = any(w in text for w in _BILLING_WORDS)
    has_quota = any(w in text for w in _QUOTA_WORDS)
    has_rate = any(w in text for w in _RATE_WORDS)
    if status == 402:
        return ErrorKind.BILLING
    if status == 429:
        if has_billing:
            return ErrorKind.BILLING
        if has_quota:
            return ErrorKind.QUOTA
        if has_rate or retry_after:
            return ErrorKind.RATE_LIMIT
        return ErrorKind.QUOTA
    if status in (401, 403):
        # 403 본문이 크레딧/결제 문제를 말하면 비용 오류로 본다 (권한 오류는 무료 한도로 단정하지 않음)
        return ErrorKind.BILLING if has_billing else ErrorKind.AUTH
    if status in (400, 404, 405, 409, 413, 415, 422):
        if has_billing:
            return ErrorKind.BILLING
        if has_quota:
            return ErrorKind.QUOTA
        return ErrorKind.INVALID_REQUEST
    if status in (408, 425) or (status is not None and 500 <= status < 600):
        return ErrorKind.TRANSIENT
    if status is None:
        return ErrorKind.TRANSIENT if not (has_billing or has_quota) else (ErrorKind.BILLING if has_billing else ErrorKind.QUOTA)
    return ErrorKind.UNKNOWN


class MotionProviderError(Exception):
    def __init__(self, kind: str, message: str, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status

    @property
    def code(self) -> str:
        return f"{self.kind}:{self.status}" if self.status else self.kind


@dataclass
class MotionRequest:
    scene_number: int
    image_path: Path
    prompt: str
    clip_seconds: float
    width: int = 0               # 원하는 출력 크기 (provider 가 지원할 때만)
    height: int = 0
    seed: int | None = None


@dataclass
class MotionResult:
    data: bytes
    model: str
    content_type: str = "video/mp4"
    extra: dict = field(default_factory=dict)


class ImageToVideoProvider(ABC):
    """무료 Image-to-Video API Adapter.

    서브클래스는 free_status / official_urls / checked_at 을 공식 문서 조사 결과로 채우고
    generate() 에서 HTTP 오류를 classify_http_error 로 분류한 MotionProviderError 로 올린다.
    """

    name: str = "base"
    free_status: str = FreeStatus.UNVERIFIED
    checked_at: str = ""                       # 공식 문서 조사 기준일
    official_urls: tuple[str, ...] = ()
    official_free_requests_per_day: int | None = None   # 공식 무료 한도를 요청 수로 알 수 있으면 (모르면 None)
    max_clip_seconds: float | None = None      # provider 가 만들 수 있는 최대 길이
    note: str = ""

    @property
    def model(self) -> str:
        return ""

    @property
    def configured(self) -> bool:
        """키/모델 등 호출에 필요한 값이 있는가 (무료 여부와 별개)."""
        return False

    def cache_params(self) -> dict:
        """캐시 키에 들어갈 생성 파라미터."""
        return {}

    @abstractmethod
    def generate(self, request: MotionRequest) -> MotionResult:
        """영상을 생성해 bytes 로 반환. 실패하면 MotionProviderError."""
