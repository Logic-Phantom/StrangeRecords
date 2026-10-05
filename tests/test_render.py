"""FFmpeg 렌더링 통합 테스트 (외부 API 없이 합성 자료로 1080x1920 MP4 생성 → 품질 검사)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.assets.audio_library import AudioLibrary
from app.assets.image_generator import render_procedural
from app.assets.synth_audio import generate_default_audio
from app.quality.checker import QualityChecker
from app.schemas import AssetInfo, Scene, SceneTiming, SubtitleChunk
from app.video.ffmpeg import probe, run_ffmpeg
from app.video.renderer import VideoRenderer


@pytest.fixture
def media(tmp_path):
    # 가로 영상(16:9) → 9:16 crop 확인용
    landscape = tmp_path / "landscape.mp4"
    run_ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=2", "-pix_fmt", "yuv420p", str(landscape)])
    image = render_procedural(tmp_path / "bg.jpg", seed="t", mood="mystery")
    narration = tmp_path / "narration.wav"
    run_ffmpeg(["-f", "lavfi", "-i", "sine=f=220:d=6", "-ac", "1", "-ar", "24000", str(narration)])
    return landscape, image, narration


def test_render_meets_youtube_shorts_spec(settings, media, tmp_path):
    landscape, image, narration = media
    generate_default_audio(settings.paths.music, settings.paths.sfx)
    settings.quality.min_duration = 3  # 테스트용 짧은 영상
    settings.video.preset = "ultrafast"

    scenes = [
        Scene(scene_number=1, narration="첫 장면", visual_search_query="q", visual_type="video", sfx="impact"),
        Scene(scene_number=2, narration="둘째 장면", visual_search_query="q", visual_type="image", effect="zoom_in",
              emphasis_words=["장면"]),
    ]
    assets = [
        AssetInfo(scene_number=1, media_type="video", local_path=str(landscape), source="local", duration=2.0),
        AssetInfo(scene_number=2, media_type="image", local_path=str(image), source="procedural"),
    ]
    timings = [
        SceneTiming(scene_number=1, start=0, end=3.0, speech_start=0.2, speech_end=2.8, audio_path=""),
        SceneTiming(scene_number=2, start=3.0, end=6.0, speech_start=3.0, speech_end=6.0, audio_path=""),
    ]
    chunks = [
        SubtitleChunk(index=1, start=0.2, end=2.8, text="첫 장면", scene_number=1),
        SubtitleChunk(index=2, start=3.0, end=5.8, text="둘째 장면", highlights=["장면"], scene_number=2),
    ]
    library = AudioLibrary(settings.paths.music, settings.paths.sfx)
    renderer = VideoRenderer(settings, library)
    out = tmp_path / "out.mp4"
    result = renderer.render(scenes, assets, timings, narration, 6.0, chunks, library.pick_bgm("dark"), "테스트 제목",
                             out, tmp_path / "work", thumbnail_path=tmp_path / "thumb.jpg")

    info = probe(out)
    assert (info.width, info.height) == (1080, 1920)
    assert round(info.fps) == 30
    assert info.video_codec == "h264" and info.audio_codec == "aac"
    assert abs(info.duration - result.duration) < 0.2
    assert result.thumbnail_path and Path(result.thumbnail_path).exists()
    assert result.sfx_used and result.sfx_used[0]["sfx"] == "impact"

    srt = tmp_path / "s.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nx\n", encoding="utf-8")
    report = QualityChecker(settings.quality).check(out, srt)
    assert report.passed, report.to_dict()


def test_quality_checker_rejects_wrong_spec(settings, tmp_path):
    bad = tmp_path / "bad.mp4"
    run_ffmpeg(["-f", "lavfi", "-i", "color=c=black:size=640x360:rate=25:duration=2", "-pix_fmt", "yuv420p", str(bad)])
    report = QualityChecker(settings.quality).check(bad)
    failed = {c.name for c in report.failures()}
    assert not report.passed
    assert {"duration", "resolution", "audio", "black_screen"} <= failed
