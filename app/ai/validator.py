"""Gemini 가 만든 대본/Scene 검증 (실패 시 ContentValidationError → 피드백과 함께 재요청)."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from app.ai.gemini_client import ContentValidationError
from app.config.settings import ScriptConfig
from app.schemas import Scene, ScriptDraft

_NON_SPEECH = re.compile(r"[\s\.,!?…·~\-\"'“”‘’()\[\]{}:;]+")
_FORBIDDEN_TTS = re.compile(r"[\(\)\[\]{}<>#*_=|@^`]|[\U0001F300-\U0001FAFF☀-➿]")


def speech_chars(text: str) -> int:
    """공백/문장부호를 제외한 글자 수 (낭독 길이 추정용)."""
    return len(_NON_SPEECH.sub("", text))


def normalize_for_compare(text: str) -> str:
    return _NON_SPEECH.sub("", text)


def estimate_seconds(text: str, chars_per_second: float) -> float:
    return speech_chars(text) / max(chars_per_second, 0.1)


def validate_script(draft: ScriptDraft, cfg: ScriptConfig) -> None:
    problems: list[str] = []
    n = speech_chars(draft.script)
    target = (cfg.min_chars + cfg.max_chars) // 2
    if n < cfg.hard_min_chars:
        add = target - n
        problems.append(
            f"script 가 너무 짧다: {n}자 (공백/문장부호 제외, 목표 {cfg.min_chars}~{cfg.max_chars}자). "
            f"약 {add}자, 즉 {max(round(add / cfg.avg_sentence_chars), 1)}문장 정도를 추가하라."
        )
    if n > cfg.hard_max_chars:
        cut = n - target
        problems.append(
            f"script 가 너무 길다: {n}자 (공백/문장부호 제외, 목표 {cfg.min_chars}~{cfg.max_chars}자). "
            f"약 {cut}자, 즉 {max(round(cut / cfg.avg_sentence_chars), 1)}문장 정도를 삭제하거나 문장을 짧게 줄여라."
        )
    if not draft.hook.strip():
        problems.append("hook 이 비어 있다.")
    elif normalize_for_compare(draft.hook)[:8] not in normalize_for_compare(draft.script)[:80]:
        problems.append("script 는 hook 문장으로 시작해야 한다.")
    if not draft.title.strip():
        problems.append("title 이 비어 있다.")
    if not draft.comment_prompt.strip():
        problems.append("comment_prompt(댓글 유도 질문)가 비어 있다.")
    for phrase in cfg.banned_phrases:
        if phrase and phrase in draft.script + draft.title:
            problems.append(f"금지 표현 사용: '{phrase}'")
    if _FORBIDDEN_TTS.search(draft.script):
        problems.append("script 에 괄호/특수기호/이모지가 있다. TTS 가 읽을 수 있는 한글 문장만 사용하라.")
    if draft.truth_status != "FACT" and not draft.truth_notice.strip():
        problems.append("truth_status 가 FACT 가 아니면 truth_notice(고지 문구)를 작성해야 한다.")
    if problems:
        raise ContentValidationError("\n".join(f"- {p}" for p in problems))


def validate_scenes(scenes: list[Scene], script: str, cfg: ScriptConfig, min_similarity: float = 0.9) -> None:
    problems: list[str] = []
    if not (cfg.min_scenes <= len(scenes) <= cfg.max_scenes):
        problems.append(f"장면 수가 {len(scenes)}개다. {cfg.min_scenes}~{cfg.max_scenes}개로 나눠라.")
    for scene in scenes:
        if not scene.narration.strip():
            problems.append(f"scene {scene.scene_number} narration 이 비어 있다.")
        if not scene.visual_search_query.strip():
            problems.append(f"scene {scene.scene_number} visual_search_query 가 비어 있다.")
        elif re.search(r"[가-힣]", scene.visual_search_query):
            problems.append(f"scene {scene.scene_number} visual_search_query 는 영어로 써야 한다.")
    joined = normalize_for_compare("".join(s.narration for s in scenes))
    ratio = SequenceMatcher(None, joined, normalize_for_compare(script)).ratio()
    if ratio < min_similarity:
        problems.append(
            f"장면 narration 을 이어 붙인 내용이 원래 대본과 다르다 (일치율 {ratio:.2f}). 대본 문장을 그대로 나눠 담아라."
        )
    if problems:
        raise ContentValidationError("\n".join(f"- {p}" for p in problems))
