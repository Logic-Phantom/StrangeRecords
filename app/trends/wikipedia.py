"""Wikimedia Pageviews API (무료 공개 API, Key 불필요) 기반 관심도 신호.

- discover: ko 위키백과 전날 인기 문서 → 채널 주제와 관련 있는 것만 주제 힌트로 사용
- keyword_signals: 후보 주제 문서의 최근 일별 조회수 → 관심도 + 증가 속도(최근 7일 / 이전 기간)
  문서를 찾으면 "공개 자료로 확인 가능" 근거도 된다 (사실 확인 가능성 점수에 사용).

API: https://wikimedia.org/api/rest_v1/ (User-Agent 필수, https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/)
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any, Callable
from urllib.parse import quote

import requests

from app.config.settings import TrendConfig
from app.trends.base import ProviderUnavailable, TrendProvider, TrendSignal, log_normalize
from app.utils.files import now_iso
from app.utils.logger import get_logger

logger = get_logger("trends.wikipedia")

API = "https://wikimedia.org/api/rest_v1/metrics/pageviews"
# 인기 문서 목록에서 늘 상위에 있는 일반 문서는 제외
_SKIP_PREFIXES = ("특수:", "Special:", "위키백과:", "Wikipedia:", "파일:", "File:", "분류:", "틀:", "포털:")
_SKIP_TITLES = {"대문", "Main_Page", "-"}


class WikipediaTrendProvider(TrendProvider):
    name = "wikipedia"

    def __init__(self, config: TrendConfig, user_agent: str, session: requests.Session | None = None,
                 title_resolver: Callable[[str, str], str | None] | None = None):
        self.config = config
        self.cfg = config.wikipedia
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.title_resolver = title_resolver
        self._last = 0.0

    def throttle(self) -> None:
        """연속 요청 간격 유지 (Wikimedia 는 짧은 시간에 몰린 요청에 429 를 준다)."""
        wait = self.cfg.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def status(self) -> tuple[bool, str]:
        if not self.cfg.enabled:
            return False, "꺼짐 (trends.wikipedia.enabled)"
        return True, "Wikimedia Pageviews API (무료, Key 불필요)"

    # ------------------------------------------------------------------ http
    def _get(self, url: str) -> dict[str, Any] | None:
        """404 → None (문서/데이터 없음). 401/403 → ProviderUnavailable. 그 외 오류는 1회 재시도 후 예외."""
        last: Exception | None = None
        for _ in range(2):
            self.throttle()
            try:
                resp = self.session.get(url, timeout=self.config.request_timeout)
            except requests.RequestException as exc:
                last = exc
                continue
            if resp.status_code == 404:
                return None
            if resp.status_code in (401, 403, 429):
                # 429: 더 보내면 같은 실행의 자료 조사(위키백과)까지 막힐 수 있으므로 이번 실행에서는 그만 보낸다
                raise ProviderUnavailable(f"Wikimedia API 거부 ({resp.status_code})")
            if resp.status_code >= 500:
                last = RuntimeError(f"Wikimedia API {resp.status_code}")
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"Wikimedia API 실패: {last}")

    # -------------------------------------------------------------- discover
    def discover(self) -> list[TrendSignal]:
        if not self.cfg.top_articles:
            return []
        day = datetime.now(timezone.utc).date() - timedelta(days=1)
        project = f"{self.config.language}.wikipedia"
        url = f"{API}/top/{project}/all-access/{day:%Y}/{day:%m}/{day:%d}"
        data = self._get(url)
        if not data:
            return []
        articles = (data.get("items") or [{}])[0].get("articles") or []
        collected = now_iso()
        signals: list[TrendSignal] = []
        for item in articles[:200]:
            title = str(item.get("article", ""))
            if not title or title in _SKIP_TITLES or title.startswith(_SKIP_PREFIXES):
                continue
            views = float(item.get("views") or 0)
            signals.append(TrendSignal(
                provider=self.name, kind="top_article", keyword=title.replace("_", " "), collected_at=collected,
                region=self.config.language, source_url=f"https://{project}.org/wiki/{quote(title)}",
                period_start=f"{day.isoformat()}T00:00:00+00:00", period_end=f"{day.isoformat()}T23:59:59+00:00",
                interest=views, interest_normalized=log_normalize(views, self.cfg.target_daily_views * 10),
                confidence=0.6, raw={"rank": item.get("rank")},
            ))
        return signals

    # ---------------------------------------------------------- per-keyword
    def keyword_signals(self, keyword: str, lang: str = "ko") -> list[TrendSignal]:
        title = keyword
        if self.title_resolver:
            try:
                self.throttle()
                title = self.title_resolver(lang, keyword) or ""
            except ProviderUnavailable:
                raise
            except Exception as exc:
                logger.debug("문서 제목 검색 실패 (%s): %s", keyword, exc)
                title = keyword
        collected = now_iso()
        if not title:
            return [TrendSignal(provider=self.name, kind="article_pageviews", keyword=keyword, collected_at=collected,
                                region=lang, status="empty", error="위키백과 문서 없음", confidence=0.0)]
        end = datetime.now(timezone.utc).date() - timedelta(days=1)
        start = end - timedelta(days=max(self.cfg.days, 8) - 1)
        project = f"{lang}.wikipedia"
        article = quote(title.replace(" ", "_"), safe="")
        url = f"{API}/per-article/{project}/all-access/user/{article}/daily/{start:%Y%m%d}00/{end:%Y%m%d}00"
        data = self._get(url)
        items = (data or {}).get("items") or []
        source_url = f"https://{project}.org/wiki/{quote(title.replace(' ', '_'))}"
        if not items:
            return [TrendSignal(provider=self.name, kind="article_pageviews", keyword=keyword, collected_at=collected,
                                region=lang, source_url=source_url, status="empty",
                                error="조회수 데이터 없음 (문서 없음 또는 신규 문서)", confidence=0.0,
                                raw={"article": title})]
        daily = [float(i.get("views") or 0) for i in items]
        recent, previous = daily[-7:], daily[:-7]
        recent_avg = mean(recent)
        prev_avg = mean(previous) if previous else None
        velocity = (recent_avg + 1) / (prev_avg + 1) if prev_avg is not None else None
        return [TrendSignal(
            provider=self.name, kind="article_pageviews", keyword=keyword, collected_at=collected, region=lang,
            source_url=source_url, period_start=f"{start.isoformat()}T00:00:00+00:00",
            period_end=f"{end.isoformat()}T23:59:59+00:00",
            interest=round(recent_avg, 1), interest_normalized=log_normalize(recent_avg, self.cfg.target_daily_views),
            velocity=round(velocity, 3) if velocity is not None else None,
            confidence=min(len(daily) / max(self.cfg.days, 1), 1.0),
            raw={"article": title, "days": len(daily), "recent_avg": recent_avg, "previous_avg": prev_avg},
        )]
