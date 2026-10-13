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
    settings.assets.cf_image.model = "@cf/stabilityai/stable-diffusion-xl-base-1.0"
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


class FlakySession(FakeSession):
    """응답 순서대로 돌려주고, Exception 이면 raise (타임아웃 흉내)."""

    def __init__(self, responses):
        super().__init__(None)
        self.responses = list(responses)

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_cloudflare_intermittent_timeouts_do_not_exclude_provider(settings, tmp_path, monkeypatch):
    import requests

    monkeypatch.setattr("app.utils.retry.random.uniform", lambda a, b: 0)
    settings.assets.cf_image.fallback_models = []  # 대체 모델 없이 provider 제외 기준만 확인
    ok, timeout = FakeResponse(content=_png((768, 1344))), requests.ReadTimeout("read timed out")
    # scene1: 타임아웃 2회(실패) → scene2 성공 → scene3: 타임아웃 2회(실패) → scene4 성공
    session = FlakySession([timeout, timeout, ok, timeout, timeout, ok])
    assets = _cf_manager(settings, session).collect(_scenes(4), tmp_path / "assets", "dark")
    assert [a.source for a in assets] == ["procedural", "cf_image", "procedural", "cf_image"]


def test_cloudflare_slow_model_falls_back_to_next_model(settings, tmp_path, monkeypatch):
    import requests

    monkeypatch.setattr("app.utils.retry.random.uniform", lambda a, b: 0)
    settings.assets.cf_image.fallback_models = [
        "@cf/bytedance/stable-diffusion-xl-lightning", "@cf/black-forest-labs/flux-1-schnell",
    ]
    timeout = requests.ReadTimeout("read timed out")
    flux = FakeResponse(content_type="application/json", payload={"result": {"image": __import__("base64").b64encode(_png((1024, 1024))).decode()}})
    # SDXL 타임아웃 2회 → Lightning 400 거부 → FLUX 성공
    session = FlakySession([timeout, timeout, FakeResponse(status_code=400), flux])
    assets = _cf_manager(settings, session).collect(_scenes(1), tmp_path / "assets", "dark")
    assert assets[0].source == "cf_image" and "flux-1-schnell" in assets[0].license
    assert [c["url"].rsplit("/run/", 1)[1] for c in session.calls] == [
        "@cf/stabilityai/stable-diffusion-xl-base-1.0", "@cf/stabilityai/stable-diffusion-xl-base-1.0",
        "@cf/bytedance/stable-diffusion-xl-lightning", "@cf/black-forest-labs/flux-1-schnell",
    ]
    assert set(session.calls[-1]["json"]) == {"prompt", "steps"}  # FLUX 는 width/height 를 보내지 않음


def _required_manager(settings, session, sleeps):
    settings.assets.cf_image.fallback_models = []
    settings.assets.ai_retry_waits = [30, 90]
    manager = _cf_manager(settings, session)
    manager.require_ai = True
    manager.sleep = sleeps.append
    return manager


def test_required_ai_image_retries_missing_scene_instead_of_procedural(settings, tmp_path, monkeypatch):
    import requests

    monkeypatch.setattr("app.utils.retry.random.uniform", lambda a, b: 0)
    ok, timeout = FakeResponse(content=_png((768, 1344))), requests.ReadTimeout("read timed out")
    # scene1 실패 → scene2,3 성공 → 30초 후 재시도 라운드에서 scene1 성공
    session = FlakySession([timeout, timeout, ok, ok, ok])
    sleeps: list[float] = []
    progress: list[list[int]] = []
    manager = _required_manager(settings, session, sleeps)
    assets = manager.collect(_scenes(3), tmp_path / "assets", "dark",
                             on_progress=lambda done: progress.append([a.scene_number for a in done]))
    assert [a.source for a in assets] == ["cf_image"] * 3 and [a.scene_number for a in assets] == [1, 2, 3]
    assert sleeps == [30] and progress[-1] == [1, 2, 3]


def test_required_ai_image_raises_instead_of_dark_screen_and_reuses_done(settings, tmp_path, monkeypatch):
    import pytest
    import requests

    from app.assets.asset_manager import MissingAIImages

    monkeypatch.setattr("app.utils.retry.random.uniform", lambda a, b: 0)
    ok, timeout = FakeResponse(content=_png((768, 1344))), requests.ReadTimeout("read timed out")
    session = FlakySession([ok] + [timeout] * 6)  # scene1 성공, scene2 는 1차 + 재시도 2라운드 모두 실패
    sleeps: list[float] = []
    saved: list = []
    manager = _required_manager(settings, session, sleeps)
    with pytest.raises(MissingAIImages, match=r"\[2\]"):
        manager.collect(_scenes(2), tmp_path / "assets", "dark", on_progress=lambda done: saved.extend(done))
    assert sleeps == [30, 90] and [a.scene_number for a in saved] == [1]

    # 다음 실행: 저장된 scene1 은 다시 그리지 않고 scene2 만 그린다
    session2 = FlakySession([ok])
    manager2 = _required_manager(settings, session2, [])
    assets = manager2.collect(_scenes(2), tmp_path / "assets", "dark", existing=saved)
    assert [a.source for a in assets] == ["cf_image", "cf_image"] and len(session2.calls) == 1


def test_required_ai_image_without_any_ai_provider_fails_fast(settings, tmp_path):
    import pytest

    settings.assets.providers = ["cf_image", "procedural"]
    manager = AssetManager(settings, build_providers(settings), require_ai=True)
    with pytest.raises(NonRetryableError, match="AI 이미지 provider"):
        manager.collect(_scenes(1), tmp_path / "assets", "dark")
