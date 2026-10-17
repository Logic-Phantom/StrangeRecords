"""장면별 최종 영상 소스 결정: 무료 AI 영상(가능한 만큼) + 나머지는 FFmpeg 움직임 효과.

순서
1. 장면 우선순위(첫 장면 Hook → 핵심 단서/증거 → 반전/결론 → 배경)와 FFmpeg 움직임 종류를 정한다 (Gemini 추가 호출 없음)
2. 이미 만든 AI 영상 재사용: 같은 작업의 이전 실행 결과(SQLite) → 같은 입력의 캐시 파일 (검증 통과한 것만)
3. 남은 장면을 우선순위대로, FreeQuotaGuard 가 허락할 때만 외부 API 호출
   - 무료 한도/크레딧/결제 오류 → 즉시 중단 (이후 요청 없음), 성공한 영상은 그대로 보존
   - 인증/모델 거부 → 그 provider 만 비활성 (재시도 없음)
   - 일시 오류/속도 제한 → 요청 수 한도 안에서 제한된 재시도, 연속 실패면 비활성
4. AI 영상이 없는 장면은 모두 FFmpeg 움직임 효과 (렌더 단계에서 적용)

이 단계의 어떤 실패도 영상 제작/업로드를 멈추지 않는다 (호출 측에서 예외를 잡아 전체 fallback).
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from app.config.settings import Settings
from app.database.repositories import MotionRepository
from app.schemas import AssetInfo, Scene, SceneTiming
from app.utils.logger import get_logger
from app.video_motion.base import ErrorKind, ImageToVideoProvider, MotionProviderError, MotionRequest
from app.video_motion.cache import MotionCache, cache_key, file_sha256
from app.video_motion.ffmpeg_fallback import plan_motions
from app.video_motion.free_quota_guard import FreeQuotaGuard
from app.video_motion.result_validator import validate_clip

logger = get_logger("motion")


class MotionStatus:
    AI_VIDEO = "ai_video_generated"
    CACHE = "cache_reused"
    FALLBACK = "fallback_motion"
    QUOTA = "quota_limited"
    DISABLED = "provider_disabled"
    FAILED = "generation_failed"
    ASSET_VIDEO = "asset_video"          # 시각자료가 이미 영상(스톡 등) → 기존 처리

    AI_SOURCES = (AI_VIDEO, CACHE)


_CLUE_WORDS = ("단서", "증거", "발견", "흔적", "기록", "사진", "편지", "지도", "목격", "남긴", "발자국", "유물")
_REVEAL_WORDS = ("반전", "사실은", "밝혀", "결국", "진실", "정체", "알고 보니", "드러", "그런데", "하지만")
_TOKEN = re.compile(r"[가-힣A-Za-z0-9]{2,}")


def scene_priorities(scenes: list[Scene], key_clues: list[str] | None = None) -> dict[int, int]:
    """1 = 첫 장면 Hook, 2 = 핵심 단서/증거, 3 = 반전/결론, 4 = 배경 설명. 무료 요청이 모자랄 때 처리 순서."""
    clue_tokens = [set(_TOKEN.findall(c)) for c in (key_clues or []) if c]
    out: dict[int, int] = {}
    for i, scene in enumerate(scenes):
        text = scene.narration
        words = set(_TOKEN.findall(text))
        if i == 0:
            out[scene.scene_number] = 1
        elif any(w in text for w in _CLUE_WORDS) or any(len(words & c) >= 2 for c in clue_tokens):
            out[scene.scene_number] = 2
        elif i == len(scenes) - 1 or any(w in text for w in _REVEAL_WORDS):
            out[scene.scene_number] = 3
        else:
            out[scene.scene_number] = 4
    return out


def motion_prompt(scene: Scene, motion: str) -> str:
    """기존 장면 설명(visual_prompt) + 움직임 지시 (새 Gemini 호출 없음)."""
    hint = {
        "slow_zoom_in": "slow cinematic camera push-in", "slow_zoom_out": "slow camera pull-back",
        "pan_left": "slow lateral camera pan to the left", "pan_right": "slow lateral camera pan to the right",
        "parallax": "gentle parallax camera drift, foreground and background moving subtly",
        "fog_overlay": "thin fog drifting slowly through the scene", "light_flicker": "soft flickering light, subtle",
        "dust_overlay": "fine dust particles floating in the air", "none": "almost still, very subtle ambient motion",
    }.get(motion, "subtle cinematic motion")
    base = (scene.visual_prompt or scene.visual or scene.visual_search_query).strip().rstrip(".")
    return f"{base}. {hint}. Keep the original composition and style, realistic motion, no text, no new objects."


@dataclass
class SceneMotion:
    scene_number: int
    priority: int
    motion: str
    status: str = MotionStatus.FALLBACK
    source: str = "ffmpeg"               # ai_video | ffmpeg | asset_video
    video_path: str = ""
    image_path: str = ""
    image_sha256: str = ""
    provider: str = ""
    model: str = ""
    cache_key: str = ""
    requested_at: str = ""
    clip_seconds: float = 0.0
    latency_s: float | None = None
    error_code: str = ""
    stop_reason: str = ""
    quota_limited: bool = False
    fallback_applied: bool = True
    cache_reused: bool = False

    def mark_ai(self, status: str, path: Path, provider: str, model: str, key: str, seconds: float) -> None:
        self.status, self.source, self.video_path = status, "ai_video", str(path)
        self.provider, self.model, self.cache_key, self.clip_seconds = provider, model, key, round(seconds, 3)
        self.fallback_applied = False
        self.cache_reused = status == MotionStatus.CACHE
        self.error_code = self.stop_reason = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MotionSummary:
    external_enabled: bool = False
    providers: list[str] = field(default_factory=list)
    blocked_providers: dict[str, str] = field(default_factory=dict)
    requests: int = 0
    requests_today: int = 0
    ai_generated: int = 0
    cache_reused: int = 0
    fallback_scenes: int = 0
    quota_limited: bool = False
    stop_reason: str = ""
    requests_after_stop: int = 0
    blocked_after_stop: int = 0
    limits: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class MotionService:
    def __init__(self, settings: Settings, repo: MotionRepository, providers: list[ImageToVideoProvider] | None = None,
                 cache: MotionCache | None = None, sleep: Callable[[float], None] = time.sleep):
        self.settings = settings
        self.cfg = settings.video_motion
        self.repo = repo
        self.providers = list(providers or [])
        self.cache = cache or MotionCache(settings.paths.motion_cache, enabled=self.cfg.cache_enabled)
        self.sleep = sleep
        self.tz = settings.app.timezone
        self._requests_after_stop = 0

    def _now(self) -> str:
        return datetime.now(ZoneInfo(self.tz)).isoformat(timespec="seconds")

    def _key(self, provider: ImageToVideoProvider, sha: str, prompt: str, seconds: float) -> str:
        v = self.settings.video
        return cache_key(sha, prompt, provider.name, provider.model, seconds, v.width, v.height, v.fps,
                         provider.cache_params())

    def _valid(self, path: Path | str) -> bool:
        return validate_clip(path, min_seconds=self.cfg.min_clip_seconds).ok

    # ------------------------------------------------------------------ main
    def plan(self, job_id: str, scenes: list[Scene], assets: list[AssetInfo], timings: list[SceneTiming],
             key_clues: list[str] | None = None, external: bool = True) -> dict:
        priorities = scene_priorities(scenes, key_clues)
        motions = plan_motions(scenes, priorities)
        asset_map = {a.scene_number: a for a in assets}
        timing_map = {t.scene_number: t for t in timings}
        items: dict[int, SceneMotion] = {}
        for scene in scenes:
            item = SceneMotion(scene.scene_number, priorities[scene.scene_number], motions[scene.scene_number])
            asset = asset_map.get(scene.scene_number)
            if asset and asset.media_type == "image" and Path(asset.local_path).exists():
                item.image_path = asset.local_path
                item.image_sha256 = file_sha256(asset.local_path)
            else:
                item.status, item.source, item.fallback_applied = MotionStatus.ASSET_VIDEO, "asset_video", False
            items[scene.scene_number] = item

        guard = FreeQuotaGuard(self.cfg, requests_today=self.repo.requests_on(self._now()[:10]),
                               job_requests_before=self.repo.requests_for_job(job_id))
        summary = MotionSummary(providers=[p.name for p in self.providers], requests_today=guard.requests_today,
                                limits={"per_job": guard.job_limit(), "per_day": guard.day_limit(),
                                        "clip_seconds": guard.clip_limit(),
                                        "total_seconds": self.cfg.max_total_video_seconds_per_job})
        for p in self.providers:
            decision = guard.provider_allowed(p)
            if not decision.allowed:
                summary.blocked_providers[p.name] = decision.reason
        usable = [p for p in self.providers if p.name not in summary.blocked_providers]
        summary.external_enabled = bool(external and usable)
        if not external:
            guard.stop("외부 호출 비활성 (offline 테스트)")
        elif not usable:
            guard.stop("검증된 무료 Image-to-Video provider 없음" + (
                f" ({'; '.join(summary.blocked_providers.values())})" if summary.blocked_providers else ""))

        scene_by_no = {s.scene_number: s for s in scenes}

        def duration(n: int) -> float:
            t = timing_map.get(n)
            return (t.end - t.start) if t else 4.0

        # 1) 재사용 (외부 호출 없음): 이전 실행 결과 → 캐시
        previous = self.repo.clips_for_job(job_id)
        for n, item in items.items():
            if not item.image_path:
                continue
            row = previous.get(n)
            if (row and row.get("success") and row.get("image_sha256") == item.image_sha256
                    and row.get("video_path") and self._valid(row["video_path"])):
                item.mark_ai(MotionStatus.CACHE, Path(row["video_path"]), row.get("provider") or "", row.get("model") or "",
                             row.get("cache_key") or "", row.get("clip_seconds") or 0.0)
                continue
            for p in self.providers:
                seconds = min(duration(n), guard.clip_limit(p))
                key = self._key(p, item.image_sha256, motion_prompt(scene_by_no[n], item.motion), seconds)
                hit = self.cache.lookup(key)
                if hit and self._valid(hit):
                    item.mark_ai(MotionStatus.CACHE, hit, p.name, p.model, key, seconds)
                    break

        # 2) 외부 생성: 우선순위 순서, 가드가 허락하는 만큼만
        todo = sorted((i for i in items.values() if i.image_path and i.source != "ai_video"),
                      key=lambda i: (i.priority, i.scene_number))
        for item in todo:
            if guard.stopped:
                break
            self._generate(job_id, item, scene_by_no[item.scene_number], duration(item.scene_number), guard)

        # 3) 남은 장면 = FFmpeg 움직임 효과
        for item in items.values():
            if item.source == "ai_video" or not item.image_path:
                continue
            item.fallback_applied = True
            if guard.quota_limited and item.status == MotionStatus.FALLBACK:
                item.status, item.quota_limited = MotionStatus.QUOTA, True
            if not item.stop_reason and guard.stop_reason:
                item.stop_reason = guard.stop_reason

        summary.requests = guard.requests
        summary.quota_limited = guard.quota_limited
        summary.stop_reason = guard.stop_reason
        summary.blocked_after_stop = guard.blocked_after_stop
        summary.requests_after_stop = self._requests_after_stop
        summary.ai_generated = sum(i.status == MotionStatus.AI_VIDEO for i in items.values())
        summary.cache_reused = sum(i.status == MotionStatus.CACHE for i in items.values())
        summary.fallback_scenes = sum(i.fallback_applied for i in items.values())
        for item in items.values():
            try:
                fields = {k: v for k, v in item.to_dict().items() if k != "scene_number"}
                self.repo.upsert_clip(job_id, item.scene_number, **fields, success=item.source == "ai_video",
                                      fallback_motion=item.motion)
            except Exception as exc:  # 기록 실패는 제작을 멈추지 않음
                logger.warning("장면 %d 이미지→영상 기록 실패: %s", item.scene_number, exc)
        return {"enabled": True, "scenes": [items[s.scene_number].to_dict() for s in scenes], "summary": summary.to_dict()}

    def _generate(self, job_id: str, item: SceneMotion, scene: Scene, scene_seconds: float, guard: FreeQuotaGuard) -> None:
        prompt = motion_prompt(scene, item.motion)
        for provider in self.providers:
            if guard.stopped:
                return
            seconds = round(min(scene_seconds, guard.clip_limit(provider)), 2)
            key = self._key(provider, item.image_sha256, prompt, seconds)
            attempts = 1 + max(int(self.cfg.max_retries_per_scene), 0)
            for attempt in range(attempts):
                decision = guard.allow(provider, seconds)
                if not decision.allowed:
                    if not item.stop_reason:
                        item.stop_reason = decision.reason
                    break
                if guard.stopped:  # 방어: 중단 후 요청이 나가면 품질 검사에서 실패 처리
                    self._requests_after_stop += 1
                item.requested_at = self._now()
                guard.record_request(seconds)
                t0 = time.monotonic()
                status_code: int | None = None
                try:
                    result = provider.generate(MotionRequest(scene.scene_number, Path(item.image_path), prompt, seconds,
                                                             self.settings.video.width, self.settings.video.height))
                    outcome = "success"
                except MotionProviderError as exc:
                    result, outcome, status_code = None, exc.kind, exc.status
                    error = exc
                except Exception as exc:  # Adapter 버그 등 → 알 수 없는 오류로 보고 이 장면은 대체
                    result, outcome = None, ErrorKind.UNKNOWN
                    error = MotionProviderError(ErrorKind.UNKNOWN, f"{type(exc).__name__}: {str(exc)[:200]}")
                latency = round(time.monotonic() - t0, 2)
                item.latency_s = latency
                try:
                    self.repo.add_request(job_id, scene.scene_number, provider.name, provider.model, outcome, status_code, latency)
                except Exception as exc:
                    logger.warning("요청 기록 실패: %s", exc)

                if result is not None:
                    suffix = ".webm" if "webm" in (result.content_type or "") else ".mp4"
                    path = self.cache.store(key, result.data, suffix) if self.cache.enabled else self._store_job(job_id, item, result.data, suffix)
                    check = validate_clip(path, min_seconds=self.cfg.min_clip_seconds)
                    if check.ok:
                        guard.record_success(provider)
                        item.mark_ai(MotionStatus.AI_VIDEO, path, provider.name, result.model or provider.model, key, seconds)
                        logger.info("장면 %d AI 영상 생성 (%s, %.1f초 요청, %.1fs 소요)", scene.scene_number, provider.name, seconds, latency)
                        return
                    self.cache.discard(path)
                    error = MotionProviderError(ErrorKind.INVALID_RESULT, f"결과 영상 검증 실패: {check.reason}")
                    outcome = ErrorKind.INVALID_RESULT

                item.error_code, item.provider, item.model = error.code, provider.name, provider.model
                logger.warning("장면 %d Image-to-Video 실패 (%s, %s): %s", scene.scene_number, provider.name, error.kind, error)
                if error.kind in ErrorKind.STOP_ALL:
                    item.status, item.quota_limited = MotionStatus.QUOTA, True
                    guard.stop_for_cost(provider, f"{error.kind} ({error.status or '-'})")
                    item.stop_reason = guard.stop_reason or guard.disabled.get(provider.name, "")
                    break
                if error.kind in ErrorKind.DISABLE_PROVIDER:
                    item.status = MotionStatus.DISABLED
                    guard.disable(provider, f"{error.kind} ({error.status or '-'})")
                    break
                item.status = MotionStatus.FAILED
                disabled = guard.record_transient_failure(provider)
                if disabled or error.kind not in ErrorKind.RETRYABLE or attempt + 1 >= attempts:
                    break
                waits = self.cfg.retry_waits or [0]
                wait = waits[min(attempt, len(waits) - 1)]
                logger.info("장면 %d %.0f초 후 1회 재시도 (%d/%d)", scene.scene_number, wait, attempt + 2, attempts)
                self.sleep(wait)

    def _store_job(self, job_id: str, item: SceneMotion, data: bytes, suffix: str) -> Path:
        path = self.settings.paths.work / job_id / "motion" / f"scene_{item.scene_number:02d}_ai{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path


def fallback_plan(scenes: list[Scene], assets: list[AssetInfo], reason: str) -> dict:
    """서비스 자체가 실패했을 때: 외부 호출 없이 모든 이미지 장면에 FFmpeg 움직임 효과."""
    priorities = scene_priorities(scenes)
    motions = plan_motions(scenes, priorities)
    asset_map = {a.scene_number: a for a in assets}
    out = []
    for s in scenes:
        a = asset_map.get(s.scene_number)
        image = bool(a and a.media_type == "image")
        out.append(SceneMotion(
            s.scene_number, priorities[s.scene_number], motions[s.scene_number],
            status=MotionStatus.FALLBACK if image else MotionStatus.ASSET_VIDEO,
            source="ffmpeg" if image else "asset_video", image_path=a.local_path if image else "",
            stop_reason=reason, fallback_applied=image,
        ).to_dict())
    summary = MotionSummary(stop_reason=reason, fallback_scenes=sum(o["fallback_applied"] for o in out))
    return {"enabled": True, "scenes": out, "summary": summary.to_dict()}


def log_summary(state: dict, render: str = "") -> None:
    s = (state or {}).get("summary") or {}
    logger.info("Image-to-Video Summary")
    logger.info("- 요청 시도 수: %s (오늘 이전 %s, 한도 작업당 %s / 하루 %s)", s.get("requests", 0), s.get("requests_today", 0),
                (s.get("limits") or {}).get("per_job", "-"), (s.get("limits") or {}).get("per_day", "-"))
    logger.info("- 성공한 AI 영상 수: %s", s.get("ai_generated", 0))
    logger.info("- 캐시 재사용 수: %s", s.get("cache_reused", 0))
    logger.info("- 무료 한도 초과 여부: %s", "예" if s.get("quota_limited") else "아니오")
    logger.info("- FFmpeg fallback 장면 수: %s", s.get("fallback_scenes", 0))
    logger.info("- API 호출 중단 사유: %s", s.get("stop_reason") or "-")
    if render:
        logger.info("- 최종 렌더링 결과: %s", render)
