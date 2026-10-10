from __future__ import annotations

import io

from PIL import Image

from app.assets.asset_manager import AssetManager, build_providers
from app.assets.base import AssetRequest
from app.assets.image_generator import (
    CloudflareImageProvider,
    GeminiImageProvider,
    HuggingFaceImageProvider,
    build_image_prompt,
)
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


class HTTPError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.response = type("Resp", (), {"status_code": status_code})()


class FakeHFClient:
    def __init__(self, fail: Exception | None = None):
        self.fail = fail
        self.calls: list[dict] = []

    def text_to_image(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        if self.fail:
            raise self.fail
        return Image.new("RGB", (kwargs["width"], kwargs["height"]), (30, 30, 50))


def _hf_manager(settings, gemini_fail: Exception | None, hf_client: FakeHFClient) -> tuple[AssetManager, FakeClient]:
    settings.assets.ai_image.enabled = True
    settings.assets.providers = ["ai_image", "hf_image", "procedural"]
    settings.secrets.hf_api_key = "hf_test"
    gemini = FakeClient(fail=gemini_fail)
    providers = build_providers(settings, gemini)
    next(p for p in providers if isinstance(p, HuggingFaceImageProvider))._client = hf_client
    return AssetManager(settings, providers), gemini


def test_gemini_quota_falls_back_to_huggingface_immediately(settings, tmp_path):
    hf = FakeHFClient()
    manager, gemini = _hf_manager(settings, NonRetryableError("429 RESOURCE_EXHAUSTED limit: 0"), hf)
    assets = manager.collect(_scenes(), tmp_path / "assets", "dark", context="메리 셀레스트호")
    assert [a.source for a in assets] == ["hf_image"] * 3  # 첫 Scene 부터 HF 가 이어 받음
    assert all(a.generated_by_ai for a in assets)
    assert (assets[0].width, assets[0].height) == (768, 1344)
    assert len(gemini.prompts) == 1
    assert hf.calls[0]["model"] == "stabilityai/stable-diffusion-xl-base-1.0"
    assert "scene 1" in hf.calls[0]["prompt"] and "메리 셀레스트호" in hf.calls[0]["prompt"]


def test_huggingface_auth_error_passes_to_next_provider(settings, tmp_path):
    hf = FakeHFClient(fail=HTTPError(403))
    manager, _ = _hf_manager(settings, NonRetryableError("quota"), hf)
    assets = manager.collect(_scenes(), tmp_path / "assets", "dark")
    assert [a.source for a in assets] == ["procedural"] * 3
    assert len(hf.calls) == 1  # 권한 오류는 이번 작업에서 제외 (Scene 마다 재시도하지 않음)


def test_huggingface_raises_for_pipeline_on_failure(settings, tmp_path):
    settings.secrets.hf_api_key = "hf_test"
    provider = HuggingFaceImageProvider("hf_test", settings.assets.hf_image, client=FakeHFClient(fail=HTTPError(402)))
    try:
        provider.fetch(AssetRequest(1, "ship", "image", 4, visual_prompt="ship"), tmp_path)
    except NonRetryableError as exc:
        assert "402" in str(exc)
    else:
        raise AssertionError("HF 실패 시 예외가 나야 다음 provider 로 넘어간다")


class FakeResponse:
    def __init__(self, status_code=200, content=b"", content_type="image/png", payload=None):
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": content_type}
        self.text = str(payload or "")
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise HTTPError(self.status_code)


class FakeSession:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self.response


def _cf_manager(settings, session: FakeSession, hf_client: FakeHFClient | None = None):
    settings.assets.ai_image.enabled = True
    settings.assets.providers = ["ai_image", "cf_image", "hf_image", "procedural"]
    settings.secrets.cloudflare_account_id, settings.secrets.cloudflare_api_token = "acc123", "cf_test"
    settings.secrets.hf_api_key = "hf_test" if hf_client else ""
    providers = build_providers(settings, FakeClient(fail=NonRetryableError("429 limit: 0")))
    next(p for p in providers if isinstance(p, CloudflareImageProvider)).session = session
    if hf_client:
        next(p for p in providers if isinstance(p, HuggingFaceImageProvider))._client = hf_client
    return AssetManager(settings, providers)


def test_gemini_failure_uses_cloudflare_first(settings, tmp_path):
    session = FakeSession(FakeResponse(content=_png((768, 1344))))
    hf = FakeHFClient()
    assets = _cf_manager(settings, session, hf).collect(_scenes(), tmp_path / "assets", "dark")
    assert [a.source for a in assets] == ["cf_image"] * 3
    assert (assets[0].width, assets[0].height) == (768, 1344)
    call = session.calls[0]
    assert call["url"].endswith("/accounts/acc123/ai/run/@cf/stabilityai/stable-diffusion-xl-base-1.0")
    assert call["headers"]["Authorization"] == "Bearer cf_test"
    assert (call["json"]["width"], call["json"]["height"]) == (768, 1344)
    assert not hf.calls


def test_cloudflare_base64_json_response(settings, tmp_path):
    import base64

    payload = {"result": {"image": base64.b64encode(_png((512, 512))).decode()}}
    session = FakeSession(FakeResponse(content_type="application/json", payload=payload))
    assets = _cf_manager(settings, session).collect(_scenes(1), tmp_path / "assets", "dark")
    assert assets[0].source == "cf_image" and assets[0].width == 512


def test_cloudflare_quota_falls_back_to_huggingface(settings, tmp_path):
    session = FakeSession(FakeResponse(status_code=429, content_type="application/json", payload={"errors": ["quota"]}))
    hf = FakeHFClient()
    assets = _cf_manager(settings, session, hf).collect(_scenes(), tmp_path / "assets", "dark")
    assert [a.source for a in assets] == ["hf_image"] * 3
    assert len(session.calls) == 1  # 한도 초과는 이번 작업에서 제외


def test_huggingface_unavailable_without_key_or_offline(settings):
    hf = settings.assets.hf_image
    assert not HuggingFaceImageProvider("", hf).available
    settings.secrets.hf_api_key = "hf_test"
    offline = next(p for p in build_providers(settings, None, ai_fallback=False) if isinstance(p, HuggingFaceImageProvider))
    assert not offline.available
