"""YouTube Data API v3 기반 보조 트렌드 신호 (공식 API 만 사용, 웹페이지 수집 없음).

- discover: videos.list(chart=mostPopular, regionCode=KR) — 지역 인기 영상 차트 (1 unit).
  ※ 이 차트는 "지역/카테고리의 인기 영상 목록"일 뿐 모든 분야의 실시간 검색 트렌드가 아니다.
    채널 주제와 관련 있는 제목만 힌트로 쓰고, 조회수는 게시 후 경과 시간으로 나눈 값(시간당 조회수)으로 본다.
- keyword_signals: search.list(q, publishedAfter=최근 N일) + videos.list(statistics) —
  최근 같은 주제 영상이 얼마나 많은지(과밀도)와 시간당 조회수(관심도). search.list 는 요청당 100 units 이므로
  후보 상위 몇 개만 조회한다 (trends.youtube.search_per_run).

인증: YOUTUBE_API_KEY 가 있으면 API Key, 없으면 업로드용 token.json(youtube.readonly 범위)으로 조회한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Callable

from app.config.settings import Settings
from app.trends.base import ProviderUnavailable, TrendProvider, TrendSignal, log_normalize
from app.utils.files import now_iso
from app.utils.logger import get_logger

logger = get_logger("trends.youtube")


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def views_per_hour(views: float, published: datetime | None, now: datetime) -> float | None:
    if published is None:
        return None
    hours = max((now - published).total_seconds() / 3600, 1.0)
    return views / hours


def _core_token(keyword: str) -> str:
    """제목 일치 판단용 가장 긴 단어."""
    words = [w for w in keyword.replace("_", " ").split() if len(w) >= 2]
    return max(words, key=len) if words else keyword


class YouTubeTrendProvider(TrendProvider):
    name = "youtube"

    def __init__(self, settings: Settings, service_factory: Callable[[], Any] | None = None):
        self.settings = settings
        self.config = settings.trends
        self.cfg = settings.trends.youtube
        self._factory = service_factory
        self._service = None
        self.search_calls = 0

    def status(self) -> tuple[bool, str]:
        if not self.cfg.enabled:
            return False, "꺼짐 (trends.youtube.enabled)"
        if self._factory or self.settings.secrets.youtube_api_key:
            return True, "YouTube Data API (API Key)" if self.settings.secrets.youtube_api_key else "YouTube Data API"
        from app.youtube.auth import token_path

        if token_path(self.settings).exists():
            return True, "YouTube Data API (token.json OAuth, youtube.readonly)"
        return False, "YOUTUBE_API_KEY / token.json 없음"

    def service(self):
        if self._service is None:
            if self._factory:
                self._service = self._factory()
            else:
                from googleapiclient.discovery import build

                key = self.settings.secrets.youtube_api_key
                if key:
                    self._service = build("youtube", "v3", developerKey=key, cache_discovery=False)
                else:
                    from app.youtube.auth import load_credentials

                    creds = load_credentials(self.settings, interactive=False)
                    self._service = build("youtube", "v3", credentials=creds, cache_discovery=False)
        return self._service

    def _execute(self, request) -> dict:
        try:
            return request.execute(num_retries=1)
        except Exception as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status in (401, 403):
                # quotaExceeded / 권한 없음 → 이번 실행에서 제외 (업로드 할당량을 지키기 위해 재시도하지 않음)
                raise ProviderUnavailable(f"YouTube Data API 거부 ({status}): {str(exc)[:200]}") from exc
            raise

    # -------------------------------------------------------------- discover
    def discover(self) -> list[TrendSignal]:
        if not self.cfg.most_popular:
            return []
        data = self._execute(self.service().videos().list(
            part="snippet,statistics", chart="mostPopular", regionCode=self.config.region,
            maxResults=min(self.cfg.max_results, 50),
        ))
        now = datetime.now(timezone.utc)
        collected = now_iso()
        signals: list[TrendSignal] = []
        for item in data.get("items", []):
            snippet, stats = item.get("snippet", {}), item.get("statistics", {})
            views = int(stats.get("viewCount", 0) or 0)
            published = _parse_time(snippet.get("publishedAt"))
            vph = views_per_hour(views, published, now)
            signals.append(TrendSignal(
                provider=self.name, kind="popular_video", keyword=snippet.get("title", ""), collected_at=collected,
                region=self.config.region, source_url=f"https://www.youtube.com/watch?v={item.get('id')}",
                published_at=published.isoformat() if published else None, period_end=collected,
                total_views=views, views_per_hour=round(vph, 1) if vph is not None else None,
                interest=vph, interest_normalized=log_normalize(vph, 50000), video_count=1,
                related_terms=list(snippet.get("tags", []) or [])[:10], confidence=0.5,
                raw={"video_id": item.get("id"), "channel": snippet.get("channelTitle"),
                     "category_id": snippet.get("categoryId")},
            ))
        return signals

    # ---------------------------------------------------------- per-keyword
    def keyword_signals(self, keyword: str, lang: str = "ko") -> list[TrendSignal]:
        if self.search_calls >= self.cfg.search_per_run:
            return []
        self.search_calls += 1
        now = datetime.now(timezone.utc)
        after = (now - timedelta(days=self.cfg.recent_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        search = self._execute(self.service().search().list(
            part="snippet", q=keyword, type="video", regionCode=self.config.region, relevanceLanguage=lang,
            publishedAfter=after, maxResults=min(self.cfg.search_max_results, 50), order="relevance",
        ))
        ids = [i.get("id", {}).get("videoId") for i in search.get("items", []) if i.get("id", {}).get("videoId")]
        collected = now_iso()
        base = dict(provider=self.name, kind="keyword_videos", keyword=keyword, collected_at=collected,
                    region=self.config.region, period_start=after, period_end=collected,
                    source_url="https://developers.google.com/youtube/v3/docs/search/list")
        if not ids:
            return [TrendSignal(**base, video_count=0, total_views=0, saturation=0.0, interest_normalized=0.0,
                                status="empty", confidence=0.4)]
        videos = self._execute(self.service().videos().list(part="snippet,statistics", id=",".join(ids)))
        core = _core_token(keyword).lower()
        rates: list[float] = []
        total = 0
        matching = 0
        for item in videos.get("items", []):
            views = int(item.get("statistics", {}).get("viewCount", 0) or 0)
            total += views
            vph = views_per_hour(views, _parse_time(item.get("snippet", {}).get("publishedAt")), now)
            if vph is not None:
                rates.append(vph)
            if core and core in item.get("snippet", {}).get("title", "").lower():
                matching += 1
        med = median(rates) if rates else None
        saturation = matching / max(self.cfg.search_max_results, 1)
        return [TrendSignal(
            **base, video_count=len(ids), total_views=total,
            views_per_hour=round(med, 1) if med is not None else None,
            interest=med, interest_normalized=log_normalize(med, 2000),
            saturation=round(min(saturation, 1.0), 3), confidence=min(len(rates) / 10, 1.0),
            raw={"matching_titles": matching, "core_token": core, "quota_units": 101},
        )]
