from __future__ import annotations

import io

from PIL import Image

from app.assets.asset_manager import AssetManager, build_providers
from app.assets.base import AssetRequest
from app.assets.image_generator import GeminiImageProvider, build_image_prompt
from app.schemas import Scene
from app.utils.retry import NonRetryableError


def _png(size=(576, 1024)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (40, 40, 60)).save(buf, format="PNG")
    return buf.getvalue()


class FakeClient:
    def __init__(self, fail: Exception | None = None):
        self.fail = fail
        self.prompts: list[str] = []

    def generate_image(self, prompt, models, aspect_ratio="9:16"):
        self.prompts.append(prompt)
        if self.fail:
            raise self.fail
        return _png(), "image/png", models[0]


def _scenes(n=3) -> list[Scene]:
    return [
        Scene(scene_number=i, narration=f"문장 {i}", visual_search_query="foggy ocean", alt_search_queries=["sea"],
              visual_prompt=f"an abandoned ship deck, scene {i}")
        for i in range(1, n + 1)
    ]


def test_image_prompt_includes_scene_story_and_style():
    prompt = build_image_prompt(
        AssetRequest(1, "ship", "image", 4, visual_prompt="An empty ship in fog", mood="dark", context="메리 셀레스트호 미스터리"),
        style="cinematic photorealistic",
    )
    assert prompt.startswith("An empty ship in fog")
    assert "메리 셀레스트호" in prompt
    assert "cinematic photorealistic" in prompt
    assert "No text" in prompt


def test_ai_images_used_for_every_scene(settings, tmp_path):
    settings.assets.ai_image.enabled = True
    client = FakeClient()
    manager = AssetManager(settings, build_providers(settings, client))
    assets = manager.collect(_scenes(), tmp_path / "assets", "dark", context="메리 셀레스트호")
    assert [a.source for a in assets] == ["ai_image"] * 3
    assert all(a.generated_by_ai for a in assets)
    assert len(client.prompts) == 3 and "scene 2" in client.prompts[1]


def test_ai_image_quota_error_falls_back_without_retrying_each_scene(settings, tmp_path):
    settings.assets.ai_image.enabled = True
    settings.assets.providers = ["ai_image", "procedural"]
    client = FakeClient(fail=NonRetryableError("quota"))
    manager = AssetManager(settings, build_providers(settings, client))
    assets = manager.collect(_scenes(), tmp_path / "assets", "dark")
    assert [a.source for a in assets] == ["procedural"] * 3
    assert len(client.prompts) == 1  # 첫 실패 후 이번 작업에서 제외


def test_ai_image_disabled_without_client(settings):
    settings.assets.ai_image.enabled = True
    provider = next(p for p in build_providers(settings, None) if isinstance(p, GeminiImageProvider))
    assert not provider.available
