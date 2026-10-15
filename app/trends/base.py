"""외부 트렌드 신호 공통 구조 + Provider 인터페이스.

신호는 "관심이 있다는 근거"일 뿐이며, 공개 API 로 알 수 없는 값(검색량, CTR, 경쟁 채널 내부 지표)은 만들지 않는다.
값이 없으면 None 으로 둔다 (0 과 구분).
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from app.utils.retry import NonRetryableError


class ProviderUnavailable(NonRetryableError):
    """인증/권한/설정이 없어 이번 실행에서 이 provider 를 쓸 수 없음 (재시도하지 않는다)."""


@dataclass
class TrendSignal:
    provider: str                       # youtube | wikipedia | google_trends
    kind: str                           # popular_video | top_article | article_pageviews | keyword_videos
    keyword: str
    collected_at: str
    region: str = ""
    category: str = ""
    source_url: str = ""
    period_start: str | None = None     # 데이터가 의미하는 기간
    period_end: str | None = None
    interest: float | None = None       # provider 원래 단위 (조회수/일 등)
    interest_normalized: float | None = None  # 0~1 (provider 별로 정규화한 값)
    velocity: float | None = None       # 최근 / 이전 비율 (1.0 = 변화 없음)
    video_count: int | None = None
    total_views: int | None = None
    views_per_hour: float | None = None
    published_at: str | None = None
    related_terms: list[str] = field(default_factory=list)
    confidence: float | None = None     # 0~1 (표본 크기/데이터 완결성 기준)
    status: str = "ok"                  # ok | empty | error
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    saturation: float | None = None     # 0~1, 최근 같은 주제 영상이 얼마나 많은가 (저장은 raw 에)

    def age_hours(self, now: datetime) -> float | None:
        """데이터 기간 끝(없으면 수집 시각) 기준 경과 시간."""
        ref = self.period_end or self.collected_at
        try:
            then = datetime.fromisoformat(ref)
        except (TypeError, ValueError):
            return None
        if then.tzinfo is None and now.tzinfo is not None:
            then = then.replace(tzinfo=now.tzinfo)
        return max((now - then).total_seconds() / 3600, 0.0)

    def to_row(self, collected_date: str) -> dict[str, Any]:
        data = asdict(self)
        raw = data.pop("raw") or {}
        saturation = data.pop("saturation")
        if saturation is not None:
            raw = {**raw, "saturation": saturation}
        data["raw_json"] = raw
        data["collected_date"] = collected_date
        return data

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "TrendSignal":
        raw = row.get("raw_json") or {}
        fields = {k: row.get(k) for k in cls.__dataclass_fields__ if k not in ("raw", "saturation")}
        fields["related_terms"] = row.get("related_terms") or []
        sig = cls(**fields)
        sig.raw = raw if isinstance(raw, dict) else {}
        sig.saturation = sig.raw.get("saturation")
        return sig


def log_normalize(value: float | None, target: float) -> float | None:
    """0~target 을 log 스케일로 0~1 에 대응 (target 이상은 1)."""
    if value is None:
        return None
    if value <= 0:
        return 0.0
    return min(math.log10(value + 1) / math.log10(max(target, 2) + 1), 1.0)


def freshness_weight(age_hours: float | None, half_life_hours: float) -> float:
    if age_hours is None:
        return 0.5
    return 0.5 ** (age_hours / max(half_life_hours, 1.0))


class TrendProvider(ABC):
    name: str = "base"

    @abstractmethod
    def status(self) -> tuple[bool, str]:
        """(사용 가능 여부, 사유/설명)."""

    def discover(self) -> list[TrendSignal]:
        """채널 주제 힌트로 쓸 최근 인기 신호 (선택)."""
        return []

    def keyword_signals(self, keyword: str, lang: str = "ko") -> list[TrendSignal]:
        """특정 주제(후보)의 관심도/과밀도 신호 (선택)."""
        return []
