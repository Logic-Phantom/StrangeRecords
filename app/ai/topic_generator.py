"""Gemini 로 새로운 주제 후보를 생성한다."""

from __future__ import annotations

from app.ai.gemini_client import ContentValidationError, GeminiClient, load_prompt
from app.config.settings import CategoryConfig, Settings
from app.database.models import TopicRecord
from app.schemas import TopicCandidate, TopicCandidates
from app.utils.logger import get_logger

logger = get_logger("topic")


def format_history(history: list[TopicRecord], limit: int = 120) -> str:
    if not history:
        return "(아직 없음)"
    lines = [f"- [{h.category}] {h.title} :: {h.topic}" for h in history[:limit]]
    return "\n".join(lines)


class TopicGenerator:
    def __init__(self, client: GeminiClient, settings: Settings):
        self.client = client
        self.settings = settings

    def generate(
        self,
        category: CategoryConfig,
        history: list[TopicRecord],
        count: int | None = None,
        extra_exclusions: list[str] | None = None,
    ) -> list[TopicCandidate]:
        count = count or self.settings.topic.candidate_count
        history_text = format_history(history)
        if extra_exclusions:
            history_text += "\n" + "\n".join(f"- (이번에 중복 판정된 후보) {t}" for t in extra_exclusions)

        prompt = load_prompt(
            "topic_prompt",
            channel_name=self.settings.app.name,
            category_id=category.id,
            category_name=category.name,
            category_examples=", ".join(category.examples),
            category_guideline=category.guideline,
            history=history_text,
            count=count,
        )

        def _validate(result: TopicCandidates) -> None:
            if not result.candidates:
                raise ContentValidationError("candidates 가 비어 있다.")

        logger.info("Gemini topic generation started (%s)", category.name)
        result = self.client.generate_json(prompt, TopicCandidates, validate=_validate, temperature=1.0)
        for candidate in result.candidates:
            candidate.category = category.id
        logger.info("Gemini 주제 후보 %d개 생성", len(result.candidates))
        return result.candidates
