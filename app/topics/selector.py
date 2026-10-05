"""오늘의 주제 선정: 기존 이력 조회 → 후보 생성 → 중복 검사 → 최종 선택."""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from app.ai.topic_generator import TopicGenerator
from app.config.settings import CategoryConfig, Settings
from app.database.models import TopicRecord
from app.database.repositories import TopicRepository
from app.schemas import TopicCandidate
from app.topics.duplicate_checker import DuplicateChecker, DuplicateResult
from app.utils.logger import get_logger

logger = get_logger("selector")


class TopicSelectionError(Exception):
    pass


@dataclass
class SelectionResult:
    selected: TopicCandidate
    category: CategoryConfig
    rounds: int
    checked: list[DuplicateResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "selected": self.selected.model_dump(),
            "category": self.category.id,
            "rounds": self.rounds,
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
        }


def choose_category(settings: Settings, topics: TopicRepository, forced: str | None = None) -> CategoryConfig:
    if forced:
        category = settings.category(forced)
        if not category:
            raise TopicSelectionError(f"알 수 없는 카테고리: {forced} (config.yaml categories 확인)")
        return category
    if settings.topic.category_strategy == "random":
        return random.choice(settings.categories)
    last_used = topics.last_used_by_category()
    # 한 번도 안 쓴 카테고리 → 가장 오래전에 쓴 카테고리 순
    return sorted(settings.categories, key=lambda c: last_used.get(c.id, ""))[0]


def pick_best(results: list[DuplicateResult]) -> TopicCandidate | None:
    fresh = [r for r in results if not r.duplicate]
    if not fresh:
        return None
    # 기존 주제와 가장 덜 비슷하고, 검증 가능한(FACT) 주제를 약간 우선
    fresh.sort(key=lambda r: (r.score - (0.05 if r.candidate.truth_status == "FACT" else 0)))
    return fresh[0].candidate


class TopicSelector:
    def __init__(self, settings: Settings, generator: TopicGenerator, checker: DuplicateChecker, topics: TopicRepository):
        self.settings = settings
        self.generator = generator
        self.checker = checker
        self.topics = topics

    def select(self, forced_category: str | None = None) -> SelectionResult:
        logger.info("Loading topic history")
        history: list[TopicRecord] = self.topics.history(self.settings.topic.history_limit)
        category = choose_category(self.settings, self.topics, forced_category)
        logger.info("기존 주제 %d개, 오늘의 카테고리: %s", len(history), category.name)

        rejected: list[str] = []
        all_checked: list[DuplicateResult] = []
        for round_no in range(1, self.settings.topic.max_generation_rounds + 1):
            candidates = self.generator.generate(category, history, extra_exclusions=rejected)
            results = self.checker.check(candidates, history)
            all_checked.extend(results)
            best = pick_best(results)
            if best:
                logger.info("Topic selected: %s (%s)", best.title, best.truth_status)
                logger.info("Duplicate check passed")
                return SelectionResult(best, category, round_no, all_checked)
            rejected += [r.candidate.title for r in results]
            logger.warning("후보 %d개 모두 중복 → 새 후보 생성 (%d/%d)", len(results), round_no, self.settings.topic.max_generation_rounds)
        raise TopicSelectionError("중복되지 않는 주제를 찾지 못했습니다")
