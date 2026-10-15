"""YouTube Analytics API v2 (영상별 시청자 반응) + Data API v3 videos.list (공개 통계/게시 정보) 읽기 전용 클라이언트.

- Analytics: reports.query(ids=channel==MINE, filters=video==ID, startDate~endDate) → 기간 누적값
  필요한 OAuth 범위: yt-analytics.readonly + youtube.readonly
  (https://developers.google.com/youtube/analytics/reference/reports/query)
- Data API: videos.list(part=snippet,statistics,contentDetails) → 게시 시각, 길이, 공개 조회수/좋아요/댓글 (실시간에 가까운 공개 수치)

두 출처의 값은 다른 컬럼에 저장한다 (Analytics: views ... / Data API: public_view_count ...).
한 번의 요청이 지표 조합 때문에 거부되면(400) 호환되는 묶음으로 나눠 다시 요청하고, 그래도 실패한 지표는 누락으로 기록한다.
API 가 제공하지 않는 Shorts 전용 지표(피드 노출, 시청함/스와이프 비율)는 여기서 만들지 않는다 → studio_import.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.config.settings import Settings
from app.utils.logger import get_logger
from app.utils.retry import NonRetryableError

logger = get_logger("analytics")

ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"

# API 지표 이름 → DB 컬럼
METRIC_COLUMNS: dict[str, str] = {
    "views": "views",
    "engagedViews": "engaged_views",
    "estimatedMinutesWatched": "estimated_minutes_watched",
    "averageViewDuration": "average_view_duration",
    "averageViewPercentage": "average_view_percentage",
    "likes": "likes",
    "comments": "comments",
    "shares": "shares",
    "subscribersGained": "subscribers_gained",
    "subscribersLost": "subscribers_lost",
}
# 전체 요청이 거부될 때 나눠서 다시 요청하는 묶음
METRIC_GROUPS: list[list[str]] = [
    ["views", "estimatedMinutesWatched", "averageViewDuration", "averageViewPercentage"],
    ["likes", "comments", "shares", "subscribersGained", "subscribersLost"],
    ["engagedViews"],
]


class AnalyticsScopeError(NonRetryableError):
    """token.json 에 Analytics 읽기 권한이 없음 → 재인증 필요."""


class AnalyticsPermissionError(NonRetryableError):
    """API 비활성/권한 없음 (403) → Google Cloud Console 설정 필요."""


REAUTH_GUIDE = (
    "YouTube Analytics 읽기 권한(yt-analytics.readonly)이 token.json 에 없습니다. 업로드는 지금 토큰으로 계속 동작합니다.\n"
    "  성과 수집을 켜려면: ① Google Cloud Console 에서 'YouTube Analytics API' 사용 설정 → "
    "② OAuth 동의 화면(데이터 액세스)에 .../auth/yt-analytics.readonly 범위 추가 → "
    "③ 로컬에서 `python -m app.main auth` (업로드 + 읽기 + Analytics 권한을 한 번에 받음) → "
    "④ 새 token.json 내용으로 GitHub Secret YOUTUBE_TOKEN_JSON 갱신"
)


def token_scopes(settings: Settings) -> list[str]:
    """token.json 에 기록된 범위 (토큰 값은 읽지 않는다)."""
    from app.youtube.auth import token_path

    path = token_path(settings)
    if not path.exists():
        return []
    try:
        scopes = json.loads(path.read_text(encoding="utf-8")).get("scopes") or []
    except (ValueError, OSError):
        return []
    return scopes.split() if isinstance(scopes, str) else list(scopes)


def has_analytics_scope(settings: Settings) -> bool:
    return ANALYTICS_SCOPE in token_scopes(settings)


_DURATION = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?")


def parse_iso_duration(value: str | None) -> float | None:
    if not value:
        return None
    m = _DURATION.fullmatch(value)
    if not m:
        return None
    d, h, mi, s = m.groups()
    return int(d or 0) * 86400 + int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)


def _http_status(exc: Exception) -> int | None:
    return getattr(getattr(exc, "resp", None), "status", None)


@dataclass
class MetricResult:
    values: dict[str, float | None] = field(default_factory=dict)   # API 지표 이름 → 값 (None = 수집 못함)
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    no_data: bool = False


class YouTubeAnalyticsClient:
    def __init__(self, settings: Settings, analytics_service: Any = None, data_service: Any = None):
        self.settings = settings
        self._analytics = analytics_service
        self._data = data_service

    def _creds(self):
        from app.youtube.auth import load_credentials

        return load_credentials(self.settings, interactive=False)

    @property
    def data(self):
        if self._data is None:
            from googleapiclient.discovery import build

            self._data = build("youtube", "v3", credentials=self._creds(), cache_discovery=False)
        return self._data

    @property
    def analytics(self):
        if self._analytics is None:
            if not has_analytics_scope(self.settings):
                raise AnalyticsScopeError(REAUTH_GUIDE)
            from googleapiclient.discovery import build

            self._analytics = build("youtubeAnalytics", "v2", credentials=self._creds(), cache_discovery=False)
        return self._analytics

    # ------------------------------------------------------------ Data API
    def video_details(self, video_ids: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i : i + 50]
            resp = self.data.videos().list(part="snippet,statistics,contentDetails,status", id=",".join(batch)).execute(num_retries=1)
            for item in resp.get("items", []):
                stats = item.get("statistics", {})
                out[item["id"]] = {
                    "title": item.get("snippet", {}).get("title", ""),
                    "published_at": item.get("snippet", {}).get("publishedAt"),
                    "duration_seconds": parse_iso_duration(item.get("contentDetails", {}).get("duration")),
                    "privacy_status": item.get("status", {}).get("privacyStatus"),
                    "public_view_count": int(stats["viewCount"]) if "viewCount" in stats else None,
                    "public_like_count": int(stats["likeCount"]) if "likeCount" in stats else None,
                    "public_comment_count": int(stats["commentCount"]) if "commentCount" in stats else None,
                }
        return out

    # ------------------------------------------------------- Analytics API
    def _query(self, video_id: str, start: str, end: str, metrics: list[str]) -> dict[str, Any]:
        return self.analytics.reports().query(
            ids="channel==MINE", startDate=start, endDate=end, metrics=",".join(metrics),
            filters=f"video=={video_id}",
        ).execute(num_retries=1)

    @staticmethod
    def _parse(resp: dict[str, Any], metrics: list[str]) -> tuple[dict[str, float | None], bool]:
        headers = [h.get("name") for h in resp.get("columnHeaders", [])]
        rows = resp.get("rows") or []
        if not rows:
            return {m: None for m in metrics}, True
        row = rows[0]
        values = {}
        for m in metrics:
            values[m] = row[headers.index(m)] if m in headers else None
        return values, False

    def video_metrics(self, video_id: str, start: str, end: str, metrics: list[str] | None = None) -> MetricResult:
        metrics = [m for m in (metrics or list(METRIC_COLUMNS)) if m in METRIC_COLUMNS]
        result = MetricResult()
        try:
            resp = self._query(video_id, start, end, metrics)
            result.values, result.no_data = self._parse(resp, metrics)
            result.raw["all"] = resp
        except Exception as exc:
            status = _http_status(exc)
            if status in (401, 403):
                raise AnalyticsPermissionError(
                    f"YouTube Analytics API 거부 ({status}): API 사용 설정/권한을 확인하세요. {str(exc)[:200]}"
                ) from exc
            if status != 400:
                raise
            # 지표 조합 문제 → 묶음별로 다시 요청 (각 묶음 실패는 그 지표만 누락)
            logger.info("Analytics 지표 조합 거부(400) → 묶음별 재요청: %s", video_id)
            no_data = True
            for group in METRIC_GROUPS:
                group = [m for m in group if m in metrics]
                if not group:
                    continue
                try:
                    resp = self._query(video_id, start, end, group)
                    values, empty = self._parse(resp, group)
                    result.values.update(values)
                    result.raw[",".join(group)] = resp
                    no_data = no_data and empty
                except Exception as gexc:
                    if _http_status(gexc) in (401, 403):
                        raise AnalyticsPermissionError(str(gexc)[:300]) from gexc
                    result.errors.append(f"{','.join(group)}: {str(gexc)[:200]}")
                    result.values.update({m: None for m in group})
            result.no_data = no_data
        result.missing = [m for m in metrics if result.values.get(m) is None]
        return result
