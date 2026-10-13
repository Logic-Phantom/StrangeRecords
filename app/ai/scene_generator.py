"""Gemini 로 대본을 Scene 으로 분할하고 Scene 별 시각자료 검색어 + 영어 자막을 생성한다."""

from __future__ import annotations

import re

from app.ai.gemini_client import ContentValidationError, GeminiClient, load_prompt
from app.ai.validator import validate_scenes
from app.config.settings import Settings
from app.schemas import Scene, SceneList, SceneTranslations, ScriptDraft, TopicCandidate
from app.utils.logger import get_logger

logger = get_logger("scene")


class SceneGenerator:
    def __init__(self, client: GeminiClient, settings: Settings):
        self.client = client
        self.settings = settings

    def generate(self, draft: ScriptDraft, candidate: TopicCandidate) -> list[Scene]:
        cfg = self.settings.script
        category = self.settings.category(candidate.category)
        prompt = load_prompt(
            "scene_prompt",
            category_name=category.name if category else candidate.category,
            topic=candidate.topic,
            truth_status=draft.truth_status,
            script=draft.script,
            min_scenes=cfg.min_scenes,
            max_scenes=cfg.max_scenes,
            chars_per_second=cfg.chars_per_second,
            max_sfx=self.settings.audio.max_sfx,
        )
        result = self.client.generate_json(
            prompt, SceneList, validate=lambda r: validate_scenes(r.scenes, draft.script, cfg), temperature=0.6
        )
        scenes = renumber(result.scenes)
        logger.info("Scene 구성 완료: %d개", len(scenes))
        return scenes


def missing_english(scenes: list[Scene]) -> bool:
    return any(not s.narration_en.strip() for s in scenes)


def translate_scenes(client: GeminiClient, scenes: list[Scene], topic: str) -> list[Scene]:
    """narration_en 이 비어 있는 Scene 을 영어로 번역해 채운다 (Scene 단계에서 빠졌거나 이전 버전에서 만든 작업)."""
    numbers = {s.scene_number for s in scenes}
    prompt = load_prompt(
        "translate_prompt",
        topic=topic,
        scenes="\n".join(f"{s.scene_number}. {s.narration}" for s in scenes),
    )

    def _validate(result: SceneTranslations) -> None:
        got = {t.scene_number: t.narration_en.strip() for t in result.translations}
        problems = [f"scene {n} 번역이 없다." for n in sorted(numbers) if not got.get(n)]
        problems += [f"scene {n} 번역에 한글이 있다." for n, text in got.items() if re.search(r"[가-힣]", text)]
        if problems:
            raise ContentValidationError("\n".join(f"- {p}" for p in problems))

    result = client.generate_json(prompt, SceneTranslations, validate=_validate, temperature=0.3)
    english = {t.scene_number: t.narration_en.strip() for t in result.translations}
    for scene in scenes:
        if not scene.narration_en.strip():
            scene.narration_en = english.get(scene.scene_number, "")
    logger.info("영어 자막 번역 완료: %d개 Scene", len(scenes))
    return scenes


def renumber(scenes: list[Scene]) -> list[Scene]:
    for i, scene in enumerate(scenes, start=1):
        scene.scene_number = i
        if not scene.subtitle:
            scene.subtitle = scene.narration
    return scenes
