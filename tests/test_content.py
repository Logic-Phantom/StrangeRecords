"""대본 검증 / Gemini JSON 재요청 정책 / 메타데이터 / 예약 공개 시간."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.ai.gemini_client import ContentValidationError, GeminiClient, GeminiError
from app.ai.metadata_generator import build_hashtags, finalize_metadata, fit_tags
from app.ai.validator import speech_chars, validate_scenes, validate_script
from app.config.settings import ScriptConfig
from app.schemas import AssetInfo, ResearchNotes, Scene, ScriptDraft, ShortsContent, SourceDoc, VideoMetadata
from app.youtube.uploader import plan_publish

KST = ZoneInfo("Asia/Seoul")


def make_draft(script: str, **kw) -> ScriptDraft:
    return ScriptDraft(title="제목", hook=script.split(".")[0] + ".", script=script, comment_prompt="어떻게 생각하나요?",
                       truth_status=kw.pop("truth_status", "FACT"), **kw)


def test_speech_chars_ignores_spaces_and_punctuation():
    assert speech_chars("가 나, 다!  라?") == 4


def test_script_length_validation():
    cfg = ScriptConfig(min_seconds=40, max_seconds=54, chars_per_second=5.0)
    with pytest.raises(ContentValidationError, match="짧다"):
        validate_script(make_draft("이 배는 사라졌습니다. 끝입니다."), cfg)
    ok = "이 배는 바다 한가운데서 흔적도 없이 사라졌습니다. " + "그런데 이상한 건 지금부터입니다. " * 14
    validate_script(make_draft(ok), cfg)


def test_legend_requires_notice():
    cfg = ScriptConfig(min_seconds=1, max_seconds=100, chars_per_second=5.0)
    text = "크라켄은 바다 괴물입니다. " * 10
    with pytest.raises(ContentValidationError, match="truth_notice"):
        validate_script(make_draft(text, truth_status="LEGEND"), cfg)
    validate_script(make_draft(text, truth_status="LEGEND", truth_notice="전설로 전해지는 이야기입니다."), cfg)


def test_scene_validation():
    cfg = ScriptConfig(min_scenes=2, max_scenes=3)
    script = "첫 문장입니다. 두 번째 문장입니다."
    scenes = [Scene(scene_number=1, narration="첫 문장입니다.", visual_search_query="dark sea"),
              Scene(scene_number=2, narration="두 번째 문장입니다.", visual_search_query="old ship")]
    validate_scenes(scenes, script, cfg)
    scenes[1].visual_search_query = "오래된 배"
    with pytest.raises(ContentValidationError, match="영어"):
        validate_scenes(scenes, script, cfg)


class FakeClient(GeminiClient):
    """generate() 만 대체해 JSON 재요청 정책을 검증한다."""

    def __init__(self, responses, json_retries=2):
        from app.config.settings import GeminiConfig

        self.config = GeminiConfig(model="test", json_retries=json_retries)
        self.responses = list(responses)
        self.prompts = []

    def generate(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return self.responses.pop(0)


def test_generate_json_rerequests_with_feedback():
    client = FakeClient(["not json", '{"title": "t", "description": "d"}'])
    meta = client.generate_json("p", VideoMetadata)
    assert meta.title == "t"
    assert len(client.prompts) == 2 and "이전 응답의 문제" in client.prompts[1]


def test_generate_json_gives_up_after_two_rerequests():
    client = FakeClient(["x", "y", "z", '{"title": "t", "description": "d"}'])
    with pytest.raises(GeminiError):
        client.generate_json("p", VideoMetadata)
    assert len(client.prompts) == 3  # 최초 1회 + 재요청 2회


def test_hashtags_and_tags():
    tags = build_hashtags(["미스터리", "#미제 사건", "#shorts", "#미스터리"])
    assert tags[-1] == "#Shorts" and "#미제사건" in tags and tags.count("#미스터리") == 1
    assert sum(len(t) + 1 for t in fit_tags(["a" * 100] * 10)) <= 450


def test_finalize_metadata_adds_credits():
    content = ShortsContent(title="t", category="legendary_creatures", topic="크라켄", hook="h", script="s",
                            scenes=[], truth_status="LEGEND", truth_notice="")
    research = ResearchNotes(summary="s", sources=[SourceDoc(title="Wikipedia(ko) - 크라켄", url="https://ko.wikipedia.org/wiki/x")])
    assets = [AssetInfo(scene_number=1, media_type="video", local_path="a.mp4", source="pexels", source_id="1",
                        source_url="https://pexels.com/v/1", author="Kim")]
    meta = finalize_metadata(VideoMetadata(title="크라켄<의> 정체", description="요약", hashtags=["#크라켄"]), content, research, assets, None)
    assert "<" not in meta.title
    assert "전설" in meta.description  # LEGEND 고지
    assert "Pexels by Kim" in meta.description and "wikipedia.org" in meta.description
    assert meta.description.rstrip().endswith("#Shorts")


def test_publish_plan_scheduled(settings):
    settings.youtube.publish_mode = "scheduled"
    plan = plan_publish(settings, datetime(2026, 10, 5, 10, 30, tzinfo=KST))
    assert plan.privacy_status == "private" and plan.publish_at.hour == 12
    assert plan.publish_at_iso == "2026-10-05T03:00:00.000Z"


def test_publish_plan_late(settings):
    settings.youtube.publish_mode = "scheduled"
    settings.youtube.late_policy = "public_now"
    assert plan_publish(settings, datetime(2026, 10, 5, 12, 30, tzinfo=KST)).privacy_status == "public"
    settings.youtube.late_policy = "next_day"
    plan = plan_publish(settings, datetime(2026, 10, 5, 12, 30, tzinfo=KST))
    assert plan.publish_at.day == 6


def test_publish_plan_private(settings):
    settings.youtube.publish_mode = "private"
    plan = plan_publish(settings, datetime(2026, 10, 5, 10, 0, tzinfo=KST))
    assert plan.privacy_status == "private" and plan.publish_at is None


class _Overloaded(Exception):
    code = 503


def test_model_fallback_on_overload():
    from types import SimpleNamespace

    from app.config.settings import GeminiConfig, RetryConfig

    calls = []

    def generate_content(model, contents, config):
        calls.append(model)
        if model == "primary":
            raise _Overloaded("high demand")
        return SimpleNamespace(text="ok")

    client = GeminiClient(GeminiConfig(model="primary", fallback_models=["backup"], max_retries=2), "k",
                          RetryConfig(delays=[(0, 0)]))
    client._client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    assert client.generate("hi") == "ok"
    assert calls == ["primary", "primary", "backup"] and client.active_model == "backup"
    assert client.generate("again") == "ok" and calls[-1] == "backup"  # 전환된 모델 유지
