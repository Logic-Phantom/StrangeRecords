"""이미지→영상(video_motion) 테스트. 모든 외부 API 는 Mock (실제 유료/무료 API 를 호출하지 않음, 키 불필요)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests
from PIL import Image

from app.database.repositories import JobRepository, MotionRepository
from app.schemas import AssetInfo, Scene, SceneTiming
from app.video.ffmpeg import probe, run_ffmpeg
from app.video_motion.base import (
    ErrorKind,
    FreeStatus,
    ImageToVideoProvider,
    MotionProviderError,
    MotionResult,
    classify_http_error,
)
from app.video_motion.cache import MotionCache
from app.video_motion.free_quota_guard import FreeQuotaGuard
from app.video_motion.motion_service import MotionService, MotionStatus, scene_priorities
from app.video_motion.provider_factory import build_motion_providers
from app.video_motion.providers import ZhipuCogVideoFlashProvider
from app.video_motion.result_validator import validate_clip


# ---------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def clip_bytes(tmp_path_factory) -> bytes:
    path = tmp_path_factory.mktemp("clip") / "ai.mp4"
    run_ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=576x1024:rate=16:duration=2", "-pix_fmt", "yuv420p", str(path)])
    return path.read_bytes()


class FakeProvider(ImageToVideoProvider):
    """스크립트대로 응답하는 무료(verified_free) provider. 'ok' 또는 예외/bytes."""

    name = "fake_free"
    free_status = FreeStatus.VERIFIED_FREE

    def __init__(self, data: bytes, script: list | None = None, name: str | None = None, status: str | None = None):
        self.data = data
        self.script = list(script or [])
        self.calls: list[int] = []
        if name:
            self.name = name
        if status:
            self.free_status = status

    @property
    def model(self) -> str:
        return "fake-i2v"

    @property
    def configured(self) -> bool:
        return True

    def generate(self, request):
        self.calls.append(request.scene_number)
        outcome = self.script.pop(0) if self.script else "ok"
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, bytes):
            return MotionResult(outcome, self.model)
        return MotionResult(self.data, self.model)


def make_job(tmp_path, n=8, image=True):
    scenes, assets, timings = [], [], []
    for i in range(1, n + 1):
        path = tmp_path / f"scene_{i:02d}_cf.png"
        Image.new("RGB", (576, 1024), (30 + i * 20, 40, 60 + i * 10)).save(path)
        scenes.append(Scene(scene_number=i, narration=f"문장 {i}", visual_search_query="q",
                            visual_prompt=f"an old lighthouse in fog, scene {i}"))
        assets.append(AssetInfo(scene_number=i, media_type="image" if image else "video", local_path=str(path),
                                source="cf_image", generated_by_ai=True))
        timings.append(SceneTiming(scene_number=i, start=(i - 1) * 5.0, end=i * 5.0, speech_start=(i - 1) * 5.0,
                                   speech_end=i * 5.0, audio_path=""))
    return scenes, assets, timings


def service(settings, db, providers, sleeps=None):
    return MotionService(settings, MotionRepository(db), providers, sleep=(sleeps.append if sleeps is not None else lambda s: None))


def by_status(result) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for s in result["scenes"]:
        out.setdefault(s["status"], []).append(s["scene_number"])
    return out


# ---------------------------------------------------------------- 1. 성공
def test_success_saves_video_and_uses_it(settings, db, tmp_path, clip_bytes):
    scenes, assets, timings = make_job(tmp_path, n=3)
    provider = FakeProvider(clip_bytes)
    result = service(settings, db, [provider]).plan("job1", scenes, assets, timings)
    assert by_status(result) == {MotionStatus.AI_VIDEO: [1, 2, 3]}
    assert all(Path(s["video_path"]).exists() and s["source"] == "ai_video" for s in result["scenes"])
    assert result["summary"]["requests"] == 3 and result["summary"]["ai_generated"] == 3
    rows = MotionRepository(db).clips_for_job("job1")
    assert sorted(rows) == [1, 2, 3]
    assert all(r["success"] == 1 and r["status"] == MotionStatus.AI_VIDEO and r["cache_key"] for r in rows.values())
    assert MotionRepository(db).requests_for_job("job1") == 3


# ---------------------------------------------------------------- 2~4. 3회 성공 후 무료 한도
def test_quota_after_three_successes_stops_and_falls_back(settings, db, tmp_path, clip_bytes):
    scenes, assets, timings = make_job(tmp_path, n=8)
    quota = MotionProviderError(ErrorKind.QUOTA, "429 daily quota exceeded", 429)
    provider = FakeProvider(clip_bytes, ["ok", "ok", "ok", quota, "ok", "ok"])
    second = FakeProvider(clip_bytes, name="other_free")   # 다른 무료 provider 도 호출하지 않아야 함 (기본 quota_stop_fallback)
    result = service(settings, db, [provider, second]).plan("job1", scenes, assets, timings)

    assert len(provider.calls) == 4 and second.calls == []          # 한도 도달 이후 요청 없음
    status = by_status(result)
    assert len(status[MotionStatus.AI_VIDEO]) == 3
    assert len(status[MotionStatus.QUOTA]) == 5                      # 4번째(한도) + 남은 4개
    summary = result["summary"]
    assert summary["quota_limited"] and summary["requests"] == 4 and summary["fallback_scenes"] == 5
    assert summary["requests_after_stop"] == 0 and summary["stop_reason"]
    ai = [s for s in result["scenes"] if s["source"] == "ai_video"]
    assert all(Path(s["video_path"]).exists() for s in ai)           # 성공한 영상 보존
    assert all(s["fallback_applied"] and s["motion"] for s in result["scenes"] if s["source"] != "ai_video")
    # 첫 장면(Hook)이 가장 먼저 처리된다
    assert provider.calls[0] == 1


def test_rerun_reuses_generated_videos_without_regenerating(settings, db, tmp_path, clip_bytes):
    scenes, assets, timings = make_job(tmp_path, n=6)
    quota = MotionProviderError(ErrorKind.QUOTA, "quota", 429)
    first = FakeProvider(clip_bytes, ["ok", "ok", "ok", quota])
    r1 = service(settings, db, [first]).plan("job1", scenes, assets, timings)
    made = {s["scene_number"]: s["video_path"] for s in r1["scenes"] if s["source"] == "ai_video"}
    assert len(made) == 3

    # 실패 단계부터 다시 실행 (다음 날, 한도가 남아 있음)
    retry = FakeProvider(clip_bytes)
    settings.video_motion.max_requests_per_day = 100
    settings.video_motion.max_requests_per_job = 100
    r2 = service(settings, db, [retry]).plan("job1", scenes, assets, timings)
    reused = {s["scene_number"]: s["video_path"] for s in r2["scenes"] if s["status"] == MotionStatus.CACHE}
    assert reused == made                                             # 같은 파일 재사용
    assert not set(retry.calls) & set(made)                           # 이미 만든 장면은 다시 요청하지 않음
    assert sorted(retry.calls) == sorted(set(range(1, 7)) - set(made))


def test_rerun_reuses_from_db_record_when_cache_disabled(settings, db, tmp_path, clip_bytes):
    settings.video_motion.cache_enabled = False                       # 파일 캐시 없이 작업 기록(SQLite)만으로 재사용
    scenes, assets, timings = make_job(tmp_path, n=3)
    first = FakeProvider(clip_bytes, ["ok", MotionProviderError(ErrorKind.QUOTA, "quota", 429)])
    service(settings, db, [first]).plan("job1", scenes, assets, timings)
    rows = MotionRepository(db).clips_for_job("job1")
    assert sorted(rows) == [1, 2, 3]
    assert rows[1]["success"] == 1 and rows[2]["quota_limited"] == 1 and rows[3]["fallback_applied"] == 1
    assert rows[2]["status"] == MotionStatus.QUOTA and rows[2]["fallback_motion"]

    retry = FakeProvider(clip_bytes)
    r2 = service(settings, db, [retry]).plan("job1", scenes, assets, timings)
    assert r2["scenes"][0]["status"] == MotionStatus.CACHE and 1 not in retry.calls


# ---------------------------------------------------------------- 5. 402 / 결제 필요
@pytest.mark.parametrize("error", [
    MotionProviderError(ErrorKind.BILLING, "402 Payment Required", 402),
    MotionProviderError(classify_http_error(403, "insufficient balance, please top up"), "403", 403),
])
def test_billing_error_stops_immediately(settings, db, tmp_path, clip_bytes, error):
    scenes, assets, timings = make_job(tmp_path, n=4)
    provider = FakeProvider(clip_bytes, [error])
    result = service(settings, db, [provider]).plan("job1", scenes, assets, timings)
    assert provider.calls == [1]
    assert result["summary"]["quota_limited"] and result["summary"]["requests"] == 1
    assert set(by_status(result)) == {MotionStatus.QUOTA}


# ---------------------------------------------------------------- 6. 인증 오류
def test_auth_error_disables_provider_without_retry(settings, db, tmp_path, clip_bytes):
    scenes, assets, timings = make_job(tmp_path, n=4)
    provider = FakeProvider(clip_bytes, [MotionProviderError(ErrorKind.AUTH, "401", 401)])
    sleeps: list[float] = []
    result = service(settings, db, [provider], sleeps).plan("job1", scenes, assets, timings)
    assert provider.calls == [1] and sleeps == []
    status = by_status(result)
    assert status[MotionStatus.DISABLED] == [1]
    assert not result["summary"]["quota_limited"]                     # 인증 오류를 무료 한도로 단정하지 않음
    assert all(s["fallback_applied"] for s in result["scenes"])


# ---------------------------------------------------------------- 7. 일시 오류
def test_transient_errors_retry_limited_then_fallback(settings, db, tmp_path, clip_bytes):
    settings.video_motion.max_retries_per_scene = 1
    settings.video_motion.max_consecutive_transient_failures = 2
    scenes, assets, timings = make_job(tmp_path, n=4)
    net = MotionProviderError(ErrorKind.TRANSIENT, "timeout")
    provider = FakeProvider(clip_bytes, [net, net, net, net])
    sleeps: list[float] = []
    result = service(settings, db, [provider], sleeps).plan("job1", scenes, assets, timings)
    assert provider.calls == [1, 1]                                    # 1회 재시도 후 비활성
    assert sleeps == [settings.video_motion.retry_waits[0]]
    assert result["summary"]["requests"] == 2
    assert all(s["source"] == "ffmpeg" for s in result["scenes"])


def test_rate_limit_then_success(settings, db, tmp_path, clip_bytes):
    scenes, assets, timings = make_job(tmp_path, n=2)
    rate = MotionProviderError(classify_http_error(429, "Too Many Requests: rate limit", "5"), "429", 429)
    assert rate.kind == ErrorKind.RATE_LIMIT
    provider = FakeProvider(clip_bytes, [rate, "ok", "ok"])
    result = service(settings, db, [provider]).plan("job1", scenes, assets, timings)
    assert by_status(result) == {MotionStatus.AI_VIDEO: [1, 2]}
    assert provider.calls == [1, 1, 2]


# ---------------------------------------------------------------- 8. 손상/길이 이상
def test_corrupt_or_short_results_are_not_used(settings, db, tmp_path, clip_bytes):
    scenes, assets, timings = make_job(tmp_path, n=2)
    provider = FakeProvider(clip_bytes, [b'{"error": "not a video"}' * 1000, clip_bytes[:20000]])
    result = service(settings, db, [provider]).plan("job1", scenes, assets, timings)
    assert all(s["source"] == "ffmpeg" and s["status"] == MotionStatus.FAILED for s in result["scenes"])
    assert not list(settings.paths.motion_cache.glob("*.mp4"))         # 검증 실패 파일은 캐시에 남기지 않음

    short = tmp_path / "short.mp4"
    run_ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=576x1024:rate=16:duration=0.4", "-pix_fmt", "yuv420p", str(short)])
    assert not validate_clip(short, min_seconds=1.0).ok
    black = tmp_path / "black.mp4"
    run_ffmpeg(["-f", "lavfi", "-i", "color=c=black:size=576x1024:rate=16:duration=2", "-pix_fmt", "yuv420p", str(black)])
    assert not validate_clip(black).ok


# ---------------------------------------------------------------- 10. 같은 입력 → 캐시
def test_same_inputs_reuse_cache_across_jobs(settings, db, tmp_path, clip_bytes):
    scenes, assets, timings = make_job(tmp_path, n=2)
    service(settings, db, [FakeProvider(clip_bytes)]).plan("job1", scenes, assets, timings)
    again = FakeProvider(clip_bytes)
    result = service(settings, db, [again]).plan("job2", scenes, assets, timings)
    assert again.calls == []
    assert by_status(result) == {MotionStatus.CACHE: [1, 2]}

    settings.video_motion.cache_enabled = False
    nocache = FakeProvider(clip_bytes)
    service(settings, db, [nocache]).plan("job3", scenes, assets, timings)
    assert nocache.calls == [1, 2]


# ---------------------------------------------------------------- 11. 검증된 무료 provider 없음
def test_unverified_provider_is_never_called(settings, db, tmp_path):
    class Boom(requests.Session):
        def request(self, *a, **k):
            raise AssertionError("미검증 provider 로 요청이 나가면 안 됨")

    settings.secrets.zhipu_api_key = "test-key"
    cfg = settings.video_motion.providers[0].model_copy(update={"enabled": True})
    provider = ZhipuCogVideoFlashProvider("test-key", cfg, session=Boom())
    assert provider.free_status == FreeStatus.UNVERIFIED and provider.configured
    scenes, assets, timings = make_job(tmp_path, n=3)
    result = service(settings, db, [provider]).plan("job1", scenes, assets, timings)
    assert result["summary"]["requests"] == 0 and not result["summary"]["external_enabled"]
    assert "검증된 무료" in result["summary"]["stop_reason"]
    assert all(s["source"] == "ffmpeg" and s["motion"] for s in result["scenes"])
    assert MotionRepository(db).requests_on("2000-01-01") == 0


def test_default_config_makes_no_external_calls(settings, db, tmp_path):
    providers = build_motion_providers(settings)
    assert all(p.free_status != FreeStatus.VERIFIED_FREE or not p.configured for p in providers)


# ---------------------------------------------------------------- 13. 유료 강제 설정
def test_paid_settings_cannot_enable_paid_calls(settings, db, tmp_path, clip_bytes):
    from app.config.settings import MotionProviderConfig

    settings.video_motion.free_only = False
    settings.video_motion.paid_fallback_enabled = True
    settings.video_motion.providers = [MotionProviderConfig(name="huggingface", enabled=True),
                                       MotionProviderConfig(name="gemini_veo", enabled=True)]
    assert build_motion_providers(settings) == []                      # 유료 provider 는 만들지 않음
    guard = FreeQuotaGuard(settings.video_motion)
    assert guard.free_only and not guard.paid_fallback_enabled
    paid = FakeProvider(clip_bytes, name="paid_one", status=FreeStatus.PAID)
    scenes, assets, timings = make_job(tmp_path, n=2)
    result = service(settings, db, [paid]).plan("job1", scenes, assets, timings)
    assert paid.calls == [] and result["summary"]["requests"] == 0


def test_daily_and_job_limits(settings, db, tmp_path, clip_bytes):
    settings.video_motion.max_requests_per_job = 2
    scenes, assets, timings = make_job(tmp_path, n=4)
    provider = FakeProvider(clip_bytes)
    result = service(settings, db, [provider]).plan("job1", scenes, assets, timings)
    assert len(provider.calls) == 2 and result["summary"]["requests"] == 2
    # 하루 한도: 이미 오늘 요청한 수(DB) 포함
    settings.video_motion.max_requests_per_job = 8
    settings.video_motion.max_requests_per_day = 3
    other = FakeProvider(clip_bytes)
    second_dir = tmp_path / "b"
    second_dir.mkdir()
    s2, a2, t2 = make_job(second_dir, n=4)
    result2 = service(settings, db, [other]).plan("job2", s2, a2, t2)
    assert len(other.calls) == 1                                       # 오늘 이미 2회 → 1회만 더
    assert "하루 요청 한도" in " ".join(s["stop_reason"] for s in result2["scenes"])


def test_priority_order():
    scenes = [Scene(scene_number=i, narration=t, visual_search_query="q") for i, t in enumerate(
        ["처음 이상한 일", "마을의 배경", "남겨진 발자국이라는 단서", "다른 배경", "결국 진실이 밝혀졌다"], start=1)]
    assert scene_priorities(scenes) == {1: 1, 2: 4, 3: 2, 4: 4, 5: 3}


def test_classify_http_errors():
    assert classify_http_error(429, "") == ErrorKind.QUOTA                       # 불명확 → 비용 안전 쪽
    assert classify_http_error(429, "daily quota exceeded") == ErrorKind.QUOTA
    assert classify_http_error(429, "rate limit, slow down") == ErrorKind.RATE_LIMIT
    assert classify_http_error(429, "1113 您的账户已欠费") == ErrorKind.BILLING
    assert classify_http_error(402, "") == ErrorKind.BILLING
    assert classify_http_error(401, "invalid token") == ErrorKind.AUTH
    assert classify_http_error(503, "overloaded") == ErrorKind.TRANSIENT
    assert classify_http_error(404, "model not found") == ErrorKind.INVALID_REQUEST


# ---------------------------------------------------------------- Zhipu Adapter (Mock HTTP)
class FakeResp:
    def __init__(self, status=200, data=None, content=b"", headers=None):
        self.status_code, self._data, self.content, self.headers = status, data, content, headers or {}
        self.text = json.dumps(data) if data is not None else ""

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data

    def iter_content(self, chunk_size=1):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSession:
    def __init__(self, responses, video=b""):
        self.responses = list(responses)
        self.video = video
        self.sent: list[tuple[str, str, dict]] = []

    def request(self, method, url, headers=None, timeout=None, json=None):
        self.sent.append((method, url, headers or {}))
        return self.responses.pop(0)

    def get(self, url, stream=False, timeout=None):
        self.sent.append(("GET", url, {}))
        return FakeResp(200, content=self.video)


def test_zhipu_adapter_async_flow(settings, tmp_path, clip_bytes):
    image = tmp_path / "img.png"
    Image.new("RGB", (768, 1344), (50, 60, 70)).save(image)
    session = FakeSession([
        FakeResp(200, {"id": "task1", "task_status": "PROCESSING"}),
        FakeResp(200, {"task_status": "PROCESSING"}),
        FakeResp(200, {"task_status": "SUCCESS", "video_result": [{"url": "https://cdn.example/v.mp4"}]}),
    ], video=clip_bytes)
    cfg = settings.video_motion.providers[0].model_copy(update={"enabled": True, "poll_interval": 0})
    provider = ZhipuCogVideoFlashProvider("k", cfg, session=session, sleep=lambda s: None)
    from app.video_motion.base import MotionRequest

    result = provider.generate(MotionRequest(1, image, "fog", 4.0))
    assert result.data == clip_bytes
    assert session.sent[0][0] == "POST" and session.sent[1][1].endswith("/async-result/task1")
    assert "Authorization" not in session.sent[-1][2]                  # 결과 CDN 에는 키를 보내지 않음

    billing = FakeSession([FakeResp(429, {"error": {"code": "1113", "message": "您的账户已欠费，请充值后重试"}})])
    provider = ZhipuCogVideoFlashProvider("k", cfg, session=billing, sleep=lambda s: None)
    with pytest.raises(MotionProviderError) as exc:
        provider.generate(MotionRequest(1, image, "fog", 4.0))
    assert exc.value.kind == ErrorKind.BILLING


# ---------------------------------------------------------------- 렌더링 합성 + 품질 검사
def test_render_mixes_ai_video_and_ffmpeg_fallback(settings, db, tmp_path, clip_bytes):
    from app.assets.audio_library import AudioLibrary
    from app.assets.synth_audio import generate_default_audio
    from app.quality.checker import QualityChecker, motion_checks
    from app.schemas import SubtitleChunk
    from app.video.renderer import VideoRenderer

    generate_default_audio(settings.paths.music, settings.paths.sfx)
    settings.quality.min_duration = 3
    settings.video.preset = "ultrafast"
    scenes, assets, _ = make_job(tmp_path, n=3)
    timings = [SceneTiming(scene_number=i, start=(i - 1) * 2.5, end=i * 2.5, speech_start=(i - 1) * 2.5,
                           speech_end=i * 2.5, audio_path="") for i in (1, 2, 3)]
    quota = MotionProviderError(ErrorKind.QUOTA, "quota", 429)
    plan = service(settings, db, [FakeProvider(clip_bytes, ["ok", quota])]).plan("job1", scenes, assets, timings)
    assert [s["source"] for s in plan["scenes"]].count("ai_video") == 1

    narration = tmp_path / "n.wav"
    run_ffmpeg(["-f", "lavfi", "-i", "sine=f=220:d=7.5", "-ac", "1", "-ar", "24000", str(narration)])
    chunks = [SubtitleChunk(index=i, start=(i - 1) * 2.5, end=i * 2.5 - 0.1, text=f"자막 {i}", scene_number=i) for i in (1, 2, 3)]
    library = AudioLibrary(settings.paths.music, settings.paths.sfx)
    out = tmp_path / "out.mp4"
    result = VideoRenderer(settings, library).render(scenes, assets, timings, narration, 7.5, chunks,
                                                     library.pick_bgm("dark"), "제목", out, tmp_path / "work", motion=plan)
    assert {v["source"] for v in result.motion.values()} == {"ai_video", "ffmpeg"}
    assert result.motion[1]["source"] == "ai_video" and result.motion[1]["slowdown"] > 1.0
    info = probe(out)
    assert (info.width, info.height, round(info.fps)) == (1080, 1920, 30) and info.audio_codec == "aac"

    srt = tmp_path / "s.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nx\n", encoding="utf-8")
    report = QualityChecker(settings.quality).check(out, srt, expected_duration=result.duration)
    extra = motion_checks(plan, {str(k): v for k, v in result.motion.items()}, [1, 2, 3])
    assert report.passed, report.to_dict()
    assert extra and all(c.passed for c in extra), [c.__dict__ for c in extra]


# ---------------------------------------------------------------- 11~12. 파이프라인: 실패해도 계속
def test_pipeline_motion_step_never_fails(settings, db, tmp_path, monkeypatch):
    from app.pipeline.daily_pipeline import OPTIONAL_STEPS, STEPS, UPLOAD_STEPS, DailyPipeline, JobContext
    from app.schemas import ShortsContent

    assert STEPS.index("assets") < STEPS.index("motion") < STEPS.index("render") and "motion" in OPTIONAL_STEPS
    assert UPLOAD_STEPS == ("upload", "comment")
    pipeline = DailyPipeline(settings, db=db)
    scenes, assets, timings = make_job(tmp_path, n=2)
    job = JobRepository(db).create("job1", "production", True)
    content = ShortsContent(title="t", category="c", topic="t", hook="h", script="s", scenes=scenes)
    ctx = JobContext(job=job, state={
        "content": content.model_dump(), "assets": [a.model_dump() for a in assets],
        "narration": {"timings": [t.model_dump() for t in timings]},
    })

    def boom(*a, **k):
        raise RuntimeError("provider factory exploded")

    monkeypatch.setattr("app.video_motion.provider_factory.build_motion_providers", boom)
    pipeline.step_motion(ctx)                                         # 예외 없음
    assert ctx.state["motion"]["enabled"]
    assert all(s["source"] == "ffmpeg" for s in ctx.state["motion"]["scenes"])

    # 이미 렌더링된 작업을 재개하면 외부 호출 없이 건너뜀
    ctx2 = JobContext(job=job, state={}, completed=["render"])
    pipeline.step_motion(ctx2)
    assert ctx2.state["motion"]["enabled"] is False
