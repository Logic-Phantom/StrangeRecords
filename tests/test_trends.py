"""외부 트렌드 탐색 / 주제 점수 / 주제 선정 폴백 (외부 API 호출 없음 - 가짜 응답 사용)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import requests

from app.database.repositories import TopicRepository, TrendRepository
from app.schemas import TopicCandidate
from app.topics.duplicate_checker import DuplicateChecker, DuplicateResult
from app.topics.scoring import score_candidate, score_candidates
from app.topics.selector import TopicSelector, pick_best
from app.trends.base import ProviderUnavailable, TrendProvider, TrendSignal, freshness_weight, log_normalize
from app.trends.collector import TrendCollector, channel_lexicon, channel_relevance
from app.trends.google_trends import GoogleTrendsProvider
from app.trends.wikipedia import WikipediaTrendProvider
from app.trends.youtube import YouTubeTrendProvider


# ----------------------------------------------------------------- fakes
class FakeResponse:
    def __init__(self, status: int, data=None):
        self.status_code = status
        self._data = data

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.headers: dict = {}
        self.urls: list[str] = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        return self.handler(url)


def daily(values):
    return {"items": [{"timestamp": f"2026100{i % 9}00", "views": v} for i, v in enumerate(values)]}


def wiki(settings, handler, resolver=None):
    settings.trends.wikipedia.min_interval = 0
    return WikipediaTrendProvider(settings.trends, "test-agent", session=FakeSession(handler), title_resolver=resolver)


class StaticProvider(TrendProvider):
    def __init__(self, name, discover=None, keyword=None, error=None):
        self.name = name
        self._discover = discover or []
        self._keyword = keyword or {}
        self.error = error
        self.calls = 0

    def status(self):
        return True, "test"

    def discover(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self._discover

    def keyword_signals(self, keyword, lang="ko"):
        self.calls += 1
        if self.error:
            raise self.error
        return self._keyword.get(keyword, [])


def sig(**kw):
    base = dict(provider="wikipedia", kind="article_pageviews", keyword="k", collected_at=datetime.now().astimezone().isoformat())
    base.update(kw)
    return TrendSignal(**base)


def cand(title="메리 셀레스트호", truth="FACT", **kw):
    return TopicCandidate(title=title, topic=kw.pop("topic", f"{title} 실종 미스터리"), category="unsolved_mystery",
                          truth_status=truth, search_terms_ko=[title], **kw)


# ------------------------------------------------------- wikipedia provider
def test_wikipedia_pageviews_interest_and_velocity(settings):
    provider = wiki(settings, lambda url: FakeResponse(200, daily([10] * 23 + [40] * 7)))
    [s] = provider.keyword_signals("메리 셀레스트호")
    assert s.status == "ok" and s.interest == 40
    assert s.velocity == pytest.approx(41 / 11, rel=1e-3)  # 최근 7일 평균 ÷ 이전 평균 (+1 보정)
    assert 0 < s.interest_normalized < 1 and s.period_start and s.period_end and s.source_url.startswith("https://ko.wikipedia")


def test_wikipedia_missing_article_is_empty_not_error(settings):
    [s] = wiki(settings, lambda url: FakeResponse(404)).keyword_signals("없는 문서")
    assert s.status == "empty" and s.interest is None  # 없는 값을 0 으로 만들지 않음


def test_wikipedia_auth_failure_raises_unavailable(settings):
    with pytest.raises(ProviderUnavailable):
        wiki(settings, lambda url: FakeResponse(403)).keyword_signals("x")


def test_wikipedia_timeout_and_malformed_are_isolated_by_collector(settings):
    def timeout(url):
        raise requests.Timeout("slow")

    bad = wiki(settings, timeout)
    collector = TrendCollector(settings, providers=[bad])
    assert collector.candidate_signals([cand()]) == {0: []}
    assert collector.errors and "Timeout" in collector.errors[0] or "실패" in collector.errors[0]

    malformed = wiki(settings, lambda url: FakeResponse(200, {"unexpected": True}))
    [s] = malformed.keyword_signals("x")
    assert s.status == "empty"


def test_wikipedia_top_articles_skip_special_pages(settings):
    data = {"items": [{"articles": [{"article": "대문", "views": 9}, {"article": "특수:검색", "views": 9},
                                    {"article": "크라켄", "views": 3000, "rank": 3}]}]}
    signals = wiki(settings, lambda url: FakeResponse(200, data)).discover()
    assert [s.keyword for s in signals] == ["크라켄"] and signals[0].kind == "top_article"


# --------------------------------------------------------- youtube provider
class FakeRequest:
    def __init__(self, result):
        self.result = result

    def execute(self, num_retries=0):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeYouTube:
    def __init__(self, popular=None, search=None, videos=None, error=None):
        self.popular, self.search_items, self.video_items, self.error = popular or [], search or [], videos or [], error
        self.search_calls = 0

    def videos(self):
        outer = self

        class V:
            def list(self, **kw):
                if outer.error:
                    return FakeRequest(outer.error)
                return FakeRequest({"items": outer.popular if kw.get("chart") else outer.video_items})
        return V()

    def search(self):
        outer = self

        class S:
            def list(self, **kw):
                outer.search_calls += 1
                return FakeRequest({"items": outer.search_items})
        return S()


def _video(vid, title, views, hours_ago):
    published = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"id": vid, "snippet": {"title": title, "publishedAt": published, "tags": ["미스터리"]},
            "statistics": {"viewCount": str(views)}}


def test_youtube_most_popular_uses_age_adjusted_views(settings):
    fake = FakeYouTube(popular=[_video("a", "새 영상", 10000, 10), _video("b", "오래된 영상", 10000, 1000)])
    signals = YouTubeTrendProvider(settings, service_factory=lambda: fake).discover()
    by = {s.keyword: s for s in signals}
    assert by["새 영상"].views_per_hour > by["오래된 영상"].views_per_hour * 50  # 조회수만이 아니라 경과 시간 반영
    assert by["새 영상"].total_views == 10000 and by["새 영상"].published_at


def test_youtube_keyword_saturation_and_quota_limit(settings):
    settings.trends.youtube.search_per_run = 1
    settings.trends.youtube.search_max_results = 4
    fake = FakeYouTube(search=[{"id": {"videoId": v}} for v in "abc"],
                       videos=[_video("a", "크라켄의 정체", 500, 5), _video("b", "크라켄 실제", 100, 50), _video("c", "바다", 10, 5)])
    provider = YouTubeTrendProvider(settings, service_factory=lambda: fake)
    [s] = provider.keyword_signals("크라켄")
    assert s.video_count == 3 and s.saturation == pytest.approx(2 / 4)
    assert provider.keyword_signals("다른 주제") == []  # 하루 search.list 횟수 제한 (할당량 보호)
    assert fake.search_calls == 1


def test_youtube_quota_error_disables_provider(settings):
    class Http403(Exception):
        resp = type("R", (), {"status": 403})()

    fake = FakeYouTube(error=Http403("quotaExceeded"))
    provider = YouTubeTrendProvider(settings, service_factory=lambda: fake)
    collector = TrendCollector(settings, providers=[provider])
    assert collector.discover_hints(settings.categories[0]) == []
    assert "youtube" in collector.disabled and not collector.statuses["youtube"]["available"]


def test_google_trends_disabled_without_access(settings):
    ok, reason = GoogleTrendsProvider(settings).status()
    assert not ok and "알파" in reason
    collector = TrendCollector(settings, providers=[GoogleTrendsProvider(settings)])
    assert collector.active == []


# ---------------------------------------------------------------- collector
def test_hints_keep_only_channel_relevant_and_dedupe(settings):
    signals = [
        sig(kind="top_article", keyword="크라켄 전설", interest_normalized=0.5),
        sig(kind="top_article", keyword="크라켄 전설", interest_normalized=0.4),  # 중복
        sig(kind="top_article", keyword="아이돌 컴백 무대", interest_normalized=1.0),  # 무관한 고관심 화제
        sig(kind="top_article", keyword="미제 살인 사건", interest_normalized=0.9),  # 민감 화제 제외
    ]
    collector = TrendCollector(settings, providers=[StaticProvider("wikipedia", discover=signals)])
    hints = collector.discover_hints(settings.category("legendary_creatures"))
    assert [h.keyword for h in hints] == ["크라켄 전설"]


def test_generic_word_alone_is_not_relevant(settings):
    lex = channel_lexicon(settings)
    assert channel_relevance("정인이 사건", lex) == 0  # "사건" 하나로 관련 판정하지 않음
    assert channel_relevance("심해 생물 발견", lex) == 1.0


def test_same_day_cache_avoids_second_api_call(settings, db):
    provider = StaticProvider("wikipedia", keyword={"메리 셀레스트호": [sig(keyword="메리 셀레스트호", interest_normalized=0.3)]})
    repo = TrendRepository(db)
    TrendCollector(settings, repo, providers=[provider]).candidate_signals([cand()])
    calls = provider.calls
    got = TrendCollector(settings, repo, providers=[provider]).candidate_signals([cand()])
    assert provider.calls == calls and got[0][0].interest_normalized == 0.3  # 캐시 사용


def test_trend_disabled_returns_nothing(settings):
    settings.trends.enabled = False
    collector = TrendCollector(settings)
    assert collector.providers == [] and collector.discover_hints(settings.categories[0]) == []


# ------------------------------------------------------------------ scoring
def test_score_normalizes_and_renormalizes_missing(settings):
    s = score_candidate(DuplicateResult(cand(story_beats=["a", "b", "c"]), False, 0.1), [], settings)
    assert s.components["trend"] is None and "trend" not in s.weights_used
    assert sum(s.weights_used.values()) == pytest.approx(1.0, abs=1e-3)
    assert 0 <= s.final <= 1


def test_fresh_trend_beats_stale_trend(settings):
    now = datetime.now().astimezone()
    fresh = sig(interest_normalized=0.8, period_end=now.isoformat())
    stale = sig(interest_normalized=0.8, period_end=(now - timedelta(days=10)).isoformat())
    a = score_candidate(DuplicateResult(cand(), False, 0.1), [fresh], settings, now=now)
    b = score_candidate(DuplicateResult(cand(), False, 0.1), [stale], settings, now=now)
    assert a.components["trend"] > b.components["trend"] * 5
    assert freshness_weight(48, 48) == pytest.approx(0.5) and log_normalize(None, 10) is None


def test_popular_fiction_does_not_get_trend_bonus(settings):
    hot = sig(interest_normalized=1.0, velocity=3.0)
    s = score_candidate(DuplicateResult(cand(truth="FICTION"), False, 0.1), [hot], settings)
    assert s.components["trend"] is None and s.components["velocity"] is None
    assert any("미반영" in r for r in s.reasons)


def test_saturation_and_near_duplicate_penalties(settings):
    yt = sig(provider="youtube", kind="keyword_videos", saturation=1.0, interest_normalized=0.2)
    clean = score_candidate(DuplicateResult(cand(), False, 0.0), [], settings)
    crowded = score_candidate(DuplicateResult(cand(), False, 0.55), [yt], settings)
    assert crowded.penalties["saturation"] > 0 and crowded.penalties["duplicate"] > 0
    assert crowded.final < clean.final


def test_duplicates_never_selected(settings):
    scores = score_candidates([DuplicateResult(cand(), True, 0.9, "기존", "", "keyword")], {}, settings)
    assert scores[0].final == float("-inf")


# ----------------------------------------------------------------- selector
class FakeGenerator:
    def __init__(self, candidates):
        self.candidates = candidates
        self.kwargs = {}

    def generate(self, category, history, extra_exclusions=None, **kwargs):
        self.kwargs = kwargs
        return [c.model_copy() for c in self.candidates]


def test_selector_falls_back_to_legacy_without_signals(settings, db):
    cands = [cand("크라켄 정체", truth="LEGEND"), cand("메리 셀레스트호")]
    topics = TopicRepository(db)
    result = TopicSelector(settings, FakeGenerator(cands), DuplicateChecker(settings.duplicate), topics).select("unsolved_mystery")
    legacy = pick_best(DuplicateChecker(settings.duplicate).check([c.model_copy() for c in cands], []))
    assert result.strategy.startswith("legacy") and result.selected.title == legacy.title


def test_selector_survives_trend_failure(settings, db):
    broken = StaticProvider("wikipedia", error=RuntimeError("down"))
    collector = TrendCollector(settings, TrendRepository(db), providers=[broken])
    gen = FakeGenerator([cand()])
    result = TopicSelector(settings, gen, DuplicateChecker(settings.duplicate), TopicRepository(db), trends=collector).select()
    assert result.selected.title == "메리 셀레스트호" and collector.errors
    assert "trend_hints" in gen.kwargs  # 힌트 자리는 항상 전달 (비어 있어도)


def test_selector_uses_scores_when_signals_exist(settings, db):
    low = cand("조용한 주제", topic="조용한 주제 미스터리")
    hot = cand("심해 괴물 발견", topic="심해 생물 발견 미스터리")
    provider = StaticProvider("wikipedia", keyword={
        "조용한 주제": [sig(keyword="조용한 주제", interest_normalized=0.05, velocity=0.8)],
        "심해 괴물 발견": [sig(keyword="심해 괴물 발견", interest_normalized=0.9, velocity=2.5)],
    })
    collector = TrendCollector(settings, TrendRepository(db), providers=[provider])
    result = TopicSelector(settings, FakeGenerator([low, hot]), DuplicateChecker(settings.duplicate), TopicRepository(db),
                           trends=collector).select("unsolved_mystery")
    assert result.strategy == "scored" and result.selected.title == "심해 괴물 발견"
    row = result.to_dict()
    assert row["selection_reasons"] and row["scores"][0]["components"]


def test_missing_trend_is_imputed_not_rewarded(settings):
    """신호가 없는 후보가 신호가 있는 후보보다 유리해지지 않는다."""
    a = DuplicateResult(cand("가 주제"), False, 0.0)
    b = DuplicateResult(cand("나 주제"), False, 0.0)
    c = DuplicateResult(cand("다 주제"), False, 0.0)
    scores = score_candidates([a, b, c], {0: [sig(interest_normalized=0.6, velocity=1.5)],
                                          1: [sig(interest_normalized=0.2, velocity=1.0)]}, settings)
    assert scores[2].imputed == ["trend", "velocity"] and scores[2].components["trend"] == pytest.approx(0.4)
    assert scores[0].final > scores[2].final > scores[1].final
    assert not scores[2].has_external and scores[0].has_external


def test_wikipedia_429_stops_provider_for_run(settings):
    provider = wiki(settings, lambda url: FakeResponse(429))
    collector = TrendCollector(settings, providers=[provider])
    collector.candidate_signals([cand("가"), cand("나")])
    assert "wikipedia" in collector.disabled and len(provider.session.urls) == 1  # 더 보내지 않음
