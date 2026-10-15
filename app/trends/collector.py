"""트렌드 수집기: provider 별 실패를 격리하고, 같은 날 같은 요청은 SQLite(trend_signals) 캐시를 재사용한다.

외부 신호는 보조 기능이다. 어떤 provider 가 실패해도 예외를 밖으로 내보내지 않고 빈 결과를 돌려준다
→ 주제 선정은 기존 Gemini 후보 생성 + 중복 검사로 그대로 진행된다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from app.config.settings import CategoryConfig, Settings
from app.database.repositories import TrendRepository
from app.schemas import TopicCandidate
from app.trends.base import ProviderUnavailable, TrendProvider, TrendSignal
from app.utils.logger import get_logger
from app.utils.retry import NonRetryableError

logger = get_logger("trends")

_SPLIT = re.compile(r"[\s/·,()]+")


# 카테고리 이름을 나눴을 때 나오는 일반어 (예: "사건" 하나로 모든 실제 사건 문서가 관련 있다고 판정되지 않도록)
_GENERIC = {"사건", "및", "현실", "이야기", "역사의", "은밀한", "희귀", "전설의", "자극", "게임", "테스트", "심리테스트", "밸런스"}


def channel_lexicon(settings: Settings, category: CategoryConfig | None = None) -> list[str]:
    words = list(settings.trends.channel_keywords)
    cats = [category] if category else settings.categories
    for cat in cats:
        words += cat.examples   # "미제 사건" 같은 여러 단어 예시는 모든 단어가 있어야 일치
        words += [w for w in _SPLIT.split(cat.name) if len(w) >= 2 and w not in _GENERIC]
    seen: list[str] = []
    for w in words:
        w = w.strip().lower()
        if len(w) >= 2 and w not in seen:
            seen.append(w)
    return seen


def channel_relevance(text: str, lexicon: list[str]) -> float:
    """채널 주제 단어가 몇 개 들어 있는가 (2개 이상이면 1.0). 단순하고 설명 가능한 규칙."""
    lowered = (text or "").lower()
    matched = {w for w in lexicon if " " not in w and w in lowered}
    # "미제 사건" 처럼 띄어 쓴 예시는 단어 단위로도 확인
    for w in lexicon:
        parts = [p for p in w.split() if len(p) >= 2]
        if len(parts) > 1 and all(p in lowered for p in parts):
            matched.add(w)
    return min(len(matched) / 2, 1.0)


@dataclass
class TrendHint:
    keyword: str
    provider: str
    kind: str
    relevance: float
    interest_normalized: float | None
    source_url: str
    period_end: str | None

    def line(self) -> str:
        label = {"top_article": "위키백과 인기 문서", "popular_video": "YouTube 인기 영상"}.get(self.kind, self.kind)
        when = (self.period_end or "")[:10]
        return f"- {self.keyword} (출처: {label} {when}, 채널 관련성 {self.relevance:.2f})"

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def build_trend_providers(settings: Settings) -> list[TrendProvider]:
    from app.research.researcher import WikipediaClient
    from app.trends.google_trends import GoogleTrendsProvider
    from app.trends.wikipedia import WikipediaTrendProvider
    from app.trends.youtube import YouTubeTrendProvider

    wiki = WikipediaClient(settings.research)

    def resolve(lang: str, query: str) -> str | None:
        from app.utils.retry import RetryError

        try:
            titles = wiki.search(lang, query, limit=1)
        except RetryError as exc:
            status = getattr(getattr(exc.last_error, "response", None), "status_code", None)
            if status == 429:
                raise ProviderUnavailable("위키백과 검색 429 (요청 과다)") from exc
            raise
        return titles[0] if titles else None

    return [
        WikipediaTrendProvider(settings.trends, settings.research.user_agent, title_resolver=resolve),
        YouTubeTrendProvider(settings),
        GoogleTrendsProvider(settings),
    ]


class TrendCollector:
    def __init__(self, settings: Settings, repo: TrendRepository | None = None,
                 providers: list[TrendProvider] | None = None):
        self.settings = settings
        self.config = settings.trends
        self.repo = repo
        self.providers = providers if providers is not None else (build_trend_providers(settings) if self.config.enabled else [])
        self.statuses: dict[str, dict] = {}
        self.errors: list[str] = []
        self.disabled: set[str] = set()
        for p in self.providers:
            try:
                ok, reason = p.status()
            except Exception as exc:  # 상태 확인 실패도 격리
                ok, reason = False, f"상태 확인 실패: {exc}"
            self.statuses[p.name] = {"available": ok, "detail": reason}
            if not ok:
                self.disabled.add(p.name)
        if not self.config.enabled:
            self.statuses["_all"] = {"available": False, "detail": "trends.enabled=false"}

    @property
    def active(self) -> list[TrendProvider]:
        return [p for p in self.providers if p.name not in self.disabled]

    def _today(self) -> str:
        return datetime.now(ZoneInfo(self.settings.app.timezone)).date().isoformat()

    def _guard(self, provider: TrendProvider, what: str, fn: Callable[[], list[TrendSignal]]) -> list[TrendSignal]:
        try:
            return fn()
        except NonRetryableError as exc:  # ProviderUnavailable, 토큰 만료(YouTubeAuthError) 등 → 이번 실행에서 제외
            self.disabled.add(provider.name)
            self.statuses[provider.name] = {"available": False, "detail": str(exc)}
            self.errors.append(f"{provider.name}: {exc}")
            logger.warning("트렌드 provider %s 이번 실행에서 제외: %s", provider.name, exc)
        except Exception as exc:
            self.errors.append(f"{provider.name}/{what}: {type(exc).__name__}: {exc}")
            logger.warning("트렌드 수집 실패 (%s, %s) → 건너뜀: %s", provider.name, what, exc)
        return []

    def _store(self, signals: list[TrendSignal]) -> None:
        if not self.repo:
            return
        today = self._today()
        for s in signals:
            try:
                self.repo.save(s.to_row(today))
            except Exception as exc:
                logger.debug("트렌드 신호 저장 실패: %s", exc)

    # ---------------------------------------------------------- discover
    def discover_hints(self, category: CategoryConfig) -> list[TrendHint]:
        """채널/카테고리와 관련 있는 최근 인기 신호만 주제 힌트로 돌려준다 (관련 없는 화제는 버린다)."""
        if not self.config.enabled:
            return []
        lexicon = channel_lexicon(self.settings, category)
        excluded = [w.lower() for w in self.config.exclude_keywords]
        signals: list[TrendSignal] = []
        today = self._today()
        for provider in self.active:
            cached = []
            if self.repo:
                kinds = {"wikipedia": "top_article", "youtube": "popular_video"}
                kind = kinds.get(provider.name)
                if kind:
                    cached = [TrendSignal.from_row(r) for r in self.repo.recent(today, kind) if r["provider"] == provider.name]
            if cached:
                signals += cached
                continue
            got = self._guard(provider, "discover", provider.discover)
            self._store(got)
            signals += got
        hints: list[TrendHint] = []
        seen: set[str] = set()
        for s in signals:
            rel = channel_relevance(f"{s.keyword} {' '.join(s.related_terms)}", lexicon)
            key = s.keyword.lower()
            if rel < self.config.scoring.min_channel_fit or key in seen:
                continue
            if any(w and w in key for w in excluded):
                continue  # 실제 피해자가 있는 강력 범죄/참사 등 민감 화제는 힌트로 쓰지 않음
            seen.add(key)
            hints.append(TrendHint(s.keyword, s.provider, s.kind, rel, s.interest_normalized, s.source_url, s.period_end))
        hints.sort(key=lambda h: (h.relevance, h.interest_normalized or 0), reverse=True)
        hints = hints[: self.config.max_hints]
        logger.info("트렌드 힌트 %d개 (신호 %d개 중 채널 관련)", len(hints), len(signals))
        return hints

    # ------------------------------------------------------ per-candidate
    def _keyword(self, provider: TrendProvider, keyword: str, lang: str, kind: str) -> list[TrendSignal]:
        region = self.config.region if provider.name == "youtube" else lang
        if self.repo:
            row = self.repo.cached(provider.name, kind, keyword, region, self._today())
            if row:
                return [TrendSignal.from_row(row)]
        got = self._guard(provider, f"keyword:{keyword}", lambda: provider.keyword_signals(keyword, lang))
        for s in got:  # 캐시 키(provider, kind, keyword, region)가 조회 때와 같도록
            s.region = s.region or region
            s.kind = s.kind or kind
        self._store(got)
        return got

    def candidate_signals(self, candidates: list[TopicCandidate]) -> dict[int, list[TrendSignal]]:
        """후보별 신호. 위키백과(저렴)는 모든 후보, YouTube 검색(할당량 큼)은 앞쪽 후보만 provider 가 제한한다."""
        out: dict[int, list[TrendSignal]] = {i: [] for i in range(len(candidates))}
        if not self.config.enabled:
            return out
        for provider in self.active:
            for i, c in enumerate(candidates):
                if provider.name not in [p.name for p in self.active]:
                    break  # 실행 중 제외됨
                if provider.name == "wikipedia":
                    if i >= self.config.wikipedia.max_candidates:
                        continue
                    # 한국어 문서 우선, 없을 때만 영어 문서 (요청 수 절약)
                    got = self._keyword(provider, (c.search_terms_ko or [c.title])[0], "ko", "article_pageviews")
                    if not any(s.status == "ok" for s in got) and c.search_terms_en:
                        got += self._keyword(provider, c.search_terms_en[0], "en", "article_pageviews")
                    out[i] += got
                elif provider.name == "youtube":
                    term = (c.search_terms_ko or c.keywords or [c.title])[0]
                    out[i] += self._keyword(provider, term, "ko", "keyword_videos")
                else:
                    out[i] += self._guard(provider, "keyword", lambda: provider.keyword_signals(c.title, "ko"))
        return out

    def summary(self) -> dict:
        return {"providers": self.statuses, "errors": self.errors[:20]}
