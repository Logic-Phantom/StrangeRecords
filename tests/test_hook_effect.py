"""첫 3초 시각 Hook 효과 (실제 FFmpeg 렌더링) + 파이프라인 연결 (검수 보류 상태, 제작 조건 기록)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageStat

from app.assets.audio_library import AudioLibrary
from app.assets.image_generator import render_procedural
from app.assets.synth_audio import generate_default_audio
from app.config.settings import HookEffectConfig, VideoConfig
from app.quality.checker import QualityChecker
from app.schemas import AssetInfo, Scene, SceneTiming, ShortsContent, SubtitleChunk
from app.video import renderer as renderer_mod
from app.video.ffmpeg import probe, run_ffmpeg
from app.video.hook_effect import PRESETS, available_filters, build_hook_effect
from app.video.renderer import VideoRenderer

ALL = frozenset({"scale", "crop", "eq", "noise", "rgbashift"})


# ------------------------------------------------------------------ builder
def test_disabled_or_none_preset_returns_none():
    v = VideoConfig()
    assert build_hook_effect(HookEffectConfig(enabled=False), v, 5, available=ALL) is None
    assert build_hook_effect(HookEffectConfig(enabled=True, preset="none"), v, 5, available=ALL) is None


def test_presets_are_selectable_and_deterministic():
    v = VideoConfig()
    for preset in PRESETS:
        if preset == "none":
            continue
        a = build_hook_effect(HookEffectConfig(enabled=True, preset=preset), v, 5, seed="job", available=ALL)
        b = build_hook_effect(HookEffectConfig(enabled=True, preset=preset), v, 5, seed="job", available=ALL)
        assert a and a.filter == b.filter and a.components == PRESETS[preset]
    unknown = build_hook_effect(HookEffectConfig(enabled=True, preset="??"), v, 5, available=ALL)
    assert unknown.preset == "subtle"


def test_duration_is_limited_to_first_scene():
    plan = build_hook_effect(HookEffectConfig(enabled=True, duration_seconds=2.5), VideoConfig(), 1.6, available=ALL)
    assert plan.duration == pytest.approx(1.5)


def test_missing_filters_are_skipped_safely():
    plan = build_hook_effect(HookEffectConfig(enabled=True, preset="vhs"), VideoConfig(), 5, available=frozenset({"eq"}))
    assert plan.components == ["punch"] and "noise_vhs" in plan.skipped and "rgbashift" in plan.skipped["chroma"]
    assert build_hook_effect(HookEffectConfig(enabled=True), VideoConfig(), 5, available=frozenset()) is None


def test_allow_switches_and_glitch_has_few_spaced_pulses():
    cfg = HookEffectConfig(enabled=True, preset="glitch", allow_shake=False)
    plan = build_hook_effect(cfg, VideoConfig(), 5, seed="s", available=ALL)
    assert "shake_light" not in plan.components and plan.skipped["shake_light"] == "allow_shake=false"
    pulses = plan.filter.count("between(")
    assert 1 <= pulses <= 3  # 빠른 점멸 반복 없음


def test_ffmpeg_in_use_supports_needed_filters():
    assert ALL <= available_filters()  # imageio-ffmpeg / 시스템 FFmpeg 실제 확인


# --------------------------------------------------------------- rendering
@pytest.fixture
def scenes_media(settings, tmp_path):
    generate_default_audio(settings.paths.music, settings.paths.sfx)
    settings.quality.min_duration = 3
    settings.video.preset = "ultrafast"
    img1 = render_procedural(tmp_path / "a.jpg", seed="a", mood="dark")
    img2 = render_procedural(tmp_path / "b.jpg", seed="b", mood="mystery")
    narration = tmp_path / "n.wav"
    run_ffmpeg(["-f", "lavfi", "-i", "sine=f=220:d=7", "-ac", "1", "-ar", "24000", str(narration)])
    scenes = [Scene(scene_number=1, narration="첫 장면", visual_search_query="q", visual_type="image", effect="static"),
              Scene(scene_number=2, narration="둘째 장면", visual_search_query="q", visual_type="image", effect="static")]
    assets = [AssetInfo(scene_number=1, media_type="image", local_path=str(img1), source="procedural"),
              AssetInfo(scene_number=2, media_type="image", local_path=str(img2), source="procedural")]
    timings = [SceneTiming(scene_number=1, start=0, end=4.0, speech_start=0.2, speech_end=3.8, audio_path=""),
               SceneTiming(scene_number=2, start=4.0, end=7.0, speech_start=4.0, speech_end=7.0, audio_path="")]
    chunks = [SubtitleChunk(index=1, start=0.2, end=3.8, text="첫 장면", scene_number=1),
              SubtitleChunk(index=2, start=4.0, end=6.8, text="둘째 장면", scene_number=2)]
    return scenes, assets, timings, narration, chunks


def _render(settings, media, out: Path, work: Path):
    scenes, assets, timings, narration, chunks = media
    lib = AudioLibrary(settings.paths.music, settings.paths.sfx)
    return VideoRenderer(settings, lib).render(scenes, assets, timings, narration, 7.0, chunks, lib.pick_bgm("dark"), "제목",
                                               out, work, hook_seed="job-1")


def _frame(video: Path, t: float) -> Image.Image:
    out = video.with_name(f"{video.stem}_{t}.png")
    run_ffmpeg(["-ss", str(t), "-i", str(video), "-frames:v", "1", str(out)])
    return Image.open(out).convert("RGB")


def _diff(a: Image.Image, b: Image.Image) -> float:
    return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3


def test_hook_effect_only_changes_first_seconds(settings, scenes_media, tmp_path):
    settings.video.hook_effect = HookEffectConfig(enabled=False)
    plain = _render(settings, scenes_media, tmp_path / "plain.mp4", tmp_path / "w1")
    settings.video.hook_effect = HookEffectConfig(enabled=True, preset="vhs", duration_seconds=2.0)
    hooked = _render(settings, scenes_media, tmp_path / "hook.mp4", tmp_path / "w2")

    assert plain.hook_effect["applied"] is False and hooked.hook_effect["applied"] is True
    for r in (plain, hooked):  # 길이/해상도/코덱 유지 + 기존 품질 검사 통과
        info = probe(r.video_path)
        assert (info.width, info.height) == (1080, 1920) and abs(info.duration - plain.duration) < 0.1
        srt = tmp_path / "s.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nx\n", encoding="utf-8")
        assert QualityChecker(settings.quality).check(r.video_path, srt).passed
    early = _diff(_frame(plain.video_path, 0.8), _frame(hooked.video_path, 0.8))
    later = _diff(_frame(plain.video_path, 5.5), _frame(hooked.video_path, 5.5))
    assert early > 2.0 and later < 1.0 and early > later * 3  # 첫 장면 앞부분만 변함 (이후 장면은 원래 품질)


def test_render_falls_back_when_hook_filter_fails(settings, scenes_media, tmp_path, monkeypatch):
    from app.video.hook_effect import HookEffectPlan

    settings.video.hook_effect = HookEffectConfig(enabled=True)
    monkeypatch.setattr(renderer_mod, "build_hook_effect",
                        lambda *a, **k: HookEffectPlan(preset="subtle", duration=2.0, components=["x"], filter="nosuchfilter=1"))
    result = _render(settings, scenes_media, tmp_path / "fb.mp4", tmp_path / "w")
    assert result.hook_effect["applied"] is False and "렌더링 실패" in result.hook_effect["reason"]
    assert probe(result.video_path).width == 1080  # 효과 없이 정상 렌더링


# ----------------------------------------------------------------- pipeline
def test_review_step_is_before_upload():
    from app.pipeline.daily_pipeline import STEPS

    assert STEPS.index("metadata") < STEPS.index("review") < STEPS.index("upload")


def test_review_failure_marks_job_review_needed(settings):
    from app.database.models import JobStatus
    from app.pipeline.daily_pipeline import ContentReviewFailed, DailyPipeline, JobContext

    pipeline = DailyPipeline(settings)
    job = pipeline.jobs.create("20261020-220000", "production", True)
    pipeline._fail(JobContext(job=job), ContentReviewFailed("출처 없음"))
    assert pipeline.jobs.get(job.id).status == JobStatus.REVIEW_NEEDED


def test_production_fields_from_state(settings):
    from app.pipeline.daily_pipeline import _production_fields

    state = {
        "draft": {"hook_type": "number", "story_beats": ["의문", "반전"], "loop": True, "loop_quality": 0.9, "script_similarity": 0.1},
        "topic_selection": {"score": 0.61, "trend_sources": ["wikipedia:article_pageviews"], "strategy": "scored"},
        "render": {"duration": 52.3, "hook_effect": {"preset": "glitch", "applied": True}},
        "review": {"originality_score": 0.88, "issues": [{"code": "title_similar"}]},
        "assets": [{"source": "cf_image"}, {"source": "cf_image"}, {"source": "hf_image"}],
        "subtitles": {"chunks": [{"text_en": "hi"}]},
    }
    c = ShortsContent(title="t", category="hidden_history", topic="x", hook="h", script="s", scenes=[])
    f = _production_fields(settings, state, c, {"video_id": "abc"}, {"title_strategy": "evidence"})
    assert f["hook_effect_preset"] == "glitch" and f["visual_style"] == "cf_image" and f["subtitle_mode"] == "ko+en"
    assert f["loop_ending_enabled"] and f["topic_score"] == 0.61 and f["title_strategy"] == "evidence"
    assert f["story_structure"] == "의문 → 반전" and f["extra_json"]["review_issues"] == ["title_similar"]
