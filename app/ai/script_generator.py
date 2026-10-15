"""Gemini 로 Shorts 대본(Hook 포함)을 생성한다."""

from __future__ import annotations

from app.ai.gemini_client import GeminiClient, load_prompt
from app.ai.script_quality import apply_loop_decision, validate_script_quality
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


def format_recent_openings(previous: list[dict] | None, limit: int = 8) -> str:
    items = [p for p in (previous or []) if p.get("hook")][:limit]
    if not items:
        return "(없음)"
    return "\n".join(f"- {p['hook']}" for p in items)


class ScriptGenerator:
    def __init__(self, client: GeminiClient, settings: Settings):
        self.client = client
        self.settings = settings

    def generate(
        self,
        candidate: TopicCandidate,
        research: ResearchNotes | None,
        previous: list[dict] | None = None,
        performance_context: str = "",
    ) -> ScriptDraft:
        cfg = self.settings.script
        category = self.settings.category(candidate.category)
        loop_cfg = cfg.loop_ending
        loop_rule = (
            "이야기에 자연스럽게 맞을 때만 루프 구조를 쓰고 loop=true 로 표시한다. 억지스러우면 loop=false 로 일반 결말을 쓴다."
            if loop_cfg.enabled and loop_cfg.mode != "off" else "루프 구조를 쓰지 않는다 (loop=false)."
        )
        caution = ""
        if candidate.trend_ref:
            caution = (f"이 주제는 최근 화제('{candidate.trend_ref}')에서 착안했다. 자료 조사로 날짜와 출처가 확인된 내용만 단정하고,"
                       " 확인되지 않은 최근 내용은 '~로 알려졌습니다', '아직 확인되지 않았습니다'처럼 표현한다.")
        prompt = load_prompt(
            "script_prompt",
            channel_name=self.settings.app.name,
            category_name=category.name if category else candidate.category,
            category_guideline=category.guideline if category else "",
            topic=candidate.topic,
            hook_idea=candidate.hook_idea or "(자유)",
            story_beats=" → ".join(candidate.story_beats) or "(자유: 의문 → 배경 → 단서/반전 → 회수)",
            research=format_research(research),
            min_chars=cfg.min_chars,
            max_chars=cfg.max_chars,
            min_sentences=cfg.sentence_range[0],
            max_sentences=cfg.sentence_range[1],
            min_seconds=int(cfg.min_seconds),
            max_seconds=int(cfg.max_seconds),
            banned_phrases=", ".join(cfg.banned_phrases),
            truth_status=research.truth_status if research else candidate.truth_status,
            loop_rule=loop_rule,
            recent_openings=format_recent_openings(previous),
            performance_context=performance_context or "(성과 데이터 없음 - 기본 원칙대로)",
            extra_caution=caution or "(없음)",
        )
        quality = {}

        def _validate(d: ScriptDraft) -> None:
            validate_script(d, cfg)
            quality["q"] = validate_script_quality(d, cfg, previous)

        draft = self.client.generate_json(prompt, ScriptDraft, validate=_validate)
        apply_loop_decision(draft, quality["q"], cfg)
        for w in quality["q"].warnings:
            logger.info("대본 참고: %s", w)
        logger.info("Script generated: '%s' (%d자, hook=%s, loop=%s, %s, 최근 대본 유사도 %.2f)", draft.title,
                    speech_chars(draft.script), draft.hook_type, draft.loop, draft.truth_status, draft.script_similarity or 0)
        logger.info("루프 판정: %s", draft.loop_note)
        return draft
