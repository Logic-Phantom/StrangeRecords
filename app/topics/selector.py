"""오늘의 주제 선정.

1. 카테고리 선택 (순환 유지 + 선택적으로 채널 성과 반영)
2. 외부 신호 수집 → 채널과 관련 있는 트렌드 힌트만 후보 생성 프롬프트에 전달
3. Gemini 후보 생성
4. 기존 주제 중복 검사 (키워드 + 텍스트 + Gemini 의미 판단)
5. 후보별 외부 신호(관심도/증가 속도/과밀도/위키백과 근거) 수집
6. 채널 과거 성과와 비교 → 점수 기반 최종 선정
7. 선정 근거 기록 (topic_scores, data/topics/<job>.json)

외부 신호와 성과 데이터가 하나도 없으면 기존 방식(pick_best: 가장 덜 비슷하고 FACT 우선)으로 고른다.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.ai.topic_generator import TopicGenerator
from app.config.settings import CategoryConfig, Settings
from app.database.models import TopicRecord
from app.database.repositories import TopicRepository
from app.schemas import TopicCandidate
from app.topics.duplicate_checker import DuplicateChecker, DuplicateResult
from app.topics.scoring import TopicScore, score_candidates
from app.utils.logger import get_logger

if TYPE_CHECKING:
    from app.analytics.insights import PerformanceInsights
    from app.trends.collector import TrendCollector, TrendHint

logger = get_logger("selector")


class TopicSelectionError(Exception):
    pass


@dataclass
class SelectionResult:
    selected: TopicCandidate
    category: CategoryConfig
    rounds: int
    checked: list[DuplicateResult] = field(default_factory=list)
    scores: list[TopicScore] = field(default_factory=list)
    strategy: str = "legacy"
    hints: list["TrendHint"] = field(default_factory=list)
    trend_status: dict = field(default_factory=dict)
    category_reason: str = ""

    @property
    def selected_score(self) -> TopicScore | None:
        return next((s for s in self.scores if s.selected), None)

    def to_dict(self) -> dict:
        sel = self.selected_score
        return {
            "selected": self.selected.model_dump(),
            "category": self.category.id,
            "category_reason": self.category_reason,
            "rounds": self.rounds,
            "strategy": self.strategy,
            "selected_score": round(sel.final, 4) if sel else None,
            "selection_reasons": sel.reasons if sel else [],
            "trend_hints": [h.to_dict() for h in self.hints],
            "trend_status": self.trend_status,
            "candidates": [
                {
                    "title": r.candidate.title,
                    "topic": r.candidate.topic,
                    "duplicate": r.duplicate,
                    "similar_to": r.similar_to,
                    "method": r.method,
                    "reason": r.reason,
                    "score": round(r.score, 3),
                }
                for r in self.checked
            ],
            "scores": [s.to_row() for s in self.scores],
        }


def choose_category(settings: Settings, topics: TopicRepository, forced: str | None = None,
                    insights: "PerformanceInsights | None" = None) -> tuple[CategoryConfig, str]:
    if forced:
        category = settings.category(forced)
        if not category:
            raise TopicSelectionError(f"알 수 없는 카테고리: {forced} (config.yaml categories 확인)")
        return category, "지정"
    strategy = settings.topic.category_strategy
    if strategy == "random":
        return random.choice(settings.categories), "random"
    last_used = topics.last_used_by_category()
    # 한 번도 안 쓴 카테고리 → 가장 오래전에 쓴 카테고리 순
    ordered = sorted(settings.categories, key=lambda c: last_used.get(c.id, ""))
    if strategy != "rotate_performance":
        return ordered[0], "rotate (가장 오래 안 쓴 카테고리)"

    # 다양성: 최근에 쓴 diversity_window 개 카테고리는 제외
    window = max(min(settings.topic.diversity_window, len(ordered) - 1), 0)
    pool = ordered[: len(ordered) - window] if window else ordered
    priors = insights.category_priors() if insights else {}
    if not priors:
        return pool[0], "rotate_performance → 성과 표본 부족, 가장 오래 안 쓴 카테고리"
    n = len(pool)
    scored = []
    for rank, cat in enumerate(pool):
        staleness = 1 - rank / max(n - 1, 1)
        scored.append((0.6 * staleness + 0.4 * priors.get(cat.id, 0.5), -rank, cat))
    best = max(scored, key=lambda x: (x[0], x[1]))
    return best[2], f"rotate_performance (오래 안 쓴 정도 60% + 성과 지수 {priors.get(best[2].id, 0.5):.2f} × 40%)"


def pick_best(results: list[DuplicateResult]) -> TopicCandidate | None:
    fresh = [r for r in results if not r.duplicate]
    if not fresh:
        return None
    # 기존 주제와 가장 덜 비슷하고, 검증 가능한(FACT) 주제를 약간 우선
    fresh.sort(key=lambda r: (r.score - (0.05 if r.candidate.truth_status == "FACT" else 0)))
    return fresh[0].candidate


class TopicSelector:
    def __init__(self, settings: Settings, generator: TopicGenerator, checker: DuplicateChecker, topics: TopicRepository,
                 trends: "TrendCollector | None" = None, insights: "PerformanceInsights | None" = None):
        self.settings = settings
        self.generator = generator
        self.checker = checker
        self.topics = topics
        self.trends = trends
        self.insights = insights

    def _hints(self, category: CategoryConfig) -> list["TrendHint"]:
        if not self.trends:
            return []
        try:
            return self.trends.discover_hints(category)
        except Exception as exc:  # 보조 기능: 실패해도 기존 방식으로 진행
            logger.warning("트렌드 힌트 수집 실패 → 기존 방식으로 진행: %s", exc)
            return []

    def _score(self, results: list[DuplicateResult], category: CategoryConfig) -> list[TopicScore]:
        fresh_idx = [i for i, r in enumerate(results) if not r.duplicate]
        # YouTube 검색(할당량 큼)은 덜 비슷한 후보부터 조회되도록 정렬해서 넘긴다
        fresh_idx.sort(key=lambda i: results[i].score)
        signals_by_result: dict[int, list] = {}
        if self.trends and fresh_idx:
            try:
                got = self.trends.candidate_signals([results[i].candidate for i in fresh_idx])
                signals_by_result = {fresh_idx[j]: sigs for j, sigs in got.items()}
            except Exception as exc:
                logger.warning("후보 트렌드 신호 수집 실패 → 신호 없이 점수 계산: %s", exc)
        prior = None
        if self.insights:
            try:
                prior = self.insights.category_prior(category.id)
            except Exception as exc:
                logger.warning("성과 지수 계산 실패 → 반영하지 않음: %s", exc)
        return score_candidates(results, signals_by_result, self.settings, prior)

    def select(self, forced_category: str | None = None) -> SelectionResult:
        logger.info("Loading topic history")
        history: list[TopicRecord] = self.topics.history(self.settings.topic.history_limit)
        category, category_reason = choose_category(self.settings, self.topics, forced_category, self.insights)
        logger.info("기존 주제 %d개, 오늘의 카테고리: %s (%s)", len(history), category.name, category_reason)
        hints = self._hints(category)
        context = ""
        if self.insights:
            try:
                context = self.insights.prompt_context(category.id)
            except Exception as exc:
                logger.warning("성과 요약 실패: %s", exc)

        rejected: list[str] = []
        all_checked: list[DuplicateResult] = []
        all_scores: list[TopicScore] = []
        for round_no in range(1, self.settings.topic.max_generation_rounds + 1):
            candidates = self.generator.generate(category, history, extra_exclusions=rejected, trend_hints=hints,
                                                 performance_context=context)
            results = self.checker.check(candidates, history)
            all_checked.extend(results)
            scores = self._score(results, category)
            all_scores.extend(scores)
            fresh = [s for s in scores if not s.result.duplicate]
            if fresh:
                if any(s.has_external for s in fresh):
                    best = max(fresh, key=lambda s: s.final)
                    strategy = "scored"
                else:
                    # 외부 신호/성과 데이터 없음 → 기존 선택 규칙 그대로
                    legacy = pick_best(results)
                    best = next(s for s in fresh if s.candidate is legacy)
                    strategy = "legacy (외부 신호·성과 데이터 없음)"
                best.selected = True
                logger.info("Topic selected: %s (%s, 점수 %.3f, %s)", best.candidate.title, best.candidate.truth_status,
                            best.final, strategy)
                for reason in best.reasons:
                    logger.info("  - %s", reason)
                logger.info("Duplicate check passed")
                status = self.trends.summary() if self.trends else {"providers": {}, "errors": ["trends 비활성"]}
                return SelectionResult(best.candidate, category, round_no, all_checked, all_scores, strategy, hints,
                                       status, category_reason)
            rejected += [r.candidate.title for r in results]
            logger.warning("후보 %d개 모두 중복 → 새 후보 생성 (%d/%d)", len(results), round_no, self.settings.topic.max_generation_rounds)
        raise TopicSelectionError("중복되지 않는 주제를 찾지 못했습니다")
