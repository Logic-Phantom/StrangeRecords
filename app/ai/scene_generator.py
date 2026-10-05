"""Gemini 로 대본을 Scene 으로 분할하고 Scene 별 시각자료 검색어를 생성한다."""

from __future__ import annotations

from app.ai.gemini_client import GeminiClient, load_prompt
from app.ai.validator import validate_scenes
from app.config.settings import Settings
from app.schemas import Scene, SceneList, ScriptDraft, TopicCandidate
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


def renumber(scenes: list[Scene]) -> list[Scene]:
    for i, scene in enumerate(scenes, start=1):
        scene.scene_number = i
        if not scene.subtitle:
            scene.subtitle = scene.narration
    return scenes
