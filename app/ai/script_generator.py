"""Gemini 로 Shorts 대본(Hook 포함)을 생성한다."""

from __future__ import annotations

from app.ai.gemini_client import GeminiClient, load_prompt
from app.ai.validator import speech_chars, validate_script
from app.config.settings import Settings
from app.schemas import ResearchNotes, ScriptDraft, TopicCandidate
from app.utils.logger import get_logger

logger = get_logger("script")


def format_research(notes: ResearchNotes | None) -> str:
    if notes is None:
        return "(자료 조사 없음 - 확실하지 않은 내용은 UNCONFIRMED 표현을 사용하라)"
    lines = [f"요약: {notes.summary}", f"사실 여부: {notes.truth_status}"]
    if notes.facts:
        lines.append("사실 목록:")
        lines += [f"  - [{f.status}] {f.statement}" for f in notes.facts]
    if notes.open_questions:
        lines.append("확인되지 않은 부분:")
        lines += [f"  - {q}" for q in notes.open_questions]
    if notes.caution:
        lines.append(f"주의: {notes.caution}")
    return "\n".join(lines)


class ScriptGenerator:
    def __init__(self, client: GeminiClient, settings: Settings):
        self.client = client
        self.settings = settings

    def generate(self, candidate: TopicCandidate, research: ResearchNotes | None) -> ScriptDraft:
        cfg = self.settings.script
        category = self.settings.category(candidate.category)
        prompt = load_prompt(
            "script_prompt",
            channel_name=self.settings.app.name,
            category_name=category.name if category else candidate.category,
            category_guideline=category.guideline if category else "",
            topic=candidate.topic,
            hook_idea=candidate.hook_idea or "(자유)",
            research=format_research(research),
            min_chars=cfg.min_chars,
            max_chars=cfg.max_chars,
            min_sentences=cfg.sentence_range[0],
            max_sentences=cfg.sentence_range[1],
            min_seconds=int(cfg.min_seconds),
            max_seconds=int(cfg.max_seconds),
            banned_phrases=", ".join(cfg.banned_phrases),
            truth_status=research.truth_status if research else candidate.truth_status,
        )
        draft = self.client.generate_json(prompt, ScriptDraft, validate=lambda d: validate_script(d, cfg))
        logger.info("Script generated: '%s' (%d자, loop=%s, %s)", draft.title, speech_chars(draft.script), draft.loop, draft.truth_status)
        return draft
