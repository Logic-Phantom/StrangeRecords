"""Google Trends 공식 API 어댑터 (자리만 마련).

2026-10 현재 Google Trends API 는 알파 프로그램이다 (https://developers.google.com/search/apis/trends):
신청해서 승인받은 개발자만 쓸 수 있고, 엔드포인트/인증/응답 형식이 공개 문서에 없다.
→ 접근 권한이 없다고 가정하고 기본 비활성. 비공식 엔드포인트나 웹페이지 수집으로 우회하지 않는다.

승인을 받으면 trends.google_trends.enabled=true, endpoint, GOOGLE_TRENDS_API_KEY 를 설정하고
받은 문서의 응답 형식에 맞춰 keyword_signals() 의 파서를 구현한다. 그 전까지는 status() 가 사용 불가를 알린다.
"""

from __future__ import annotations

from app.config.settings import Settings
from app.trends.base import ProviderUnavailable, TrendProvider, TrendSignal


class GoogleTrendsProvider(TrendProvider):
    name = "google_trends"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.cfg = settings.trends.google_trends

    def status(self) -> tuple[bool, str]:
        if not self.cfg.enabled:
            return False, "꺼짐 (알파 API: 접근 권한 승인 후 trends.google_trends.enabled)"
        if not (self.cfg.endpoint and self.settings.secrets.google_trends_api_key):
            return False, "endpoint / GOOGLE_TRENDS_API_KEY 미설정"
        return False, "응답 형식 미확인 → 승인 후 받은 문서에 맞춰 파서 구현 필요"

    def keyword_signals(self, keyword: str, lang: str = "ko") -> list[TrendSignal]:
        raise ProviderUnavailable(self.status()[1])
