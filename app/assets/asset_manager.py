"""Scene 별 시각자료 수집 (config.assets.providers 순서, 기본: Gemini → Cloudflare → HF 이미지 → Pexels → Pixabay → 로컬 → 자체 생성).

assets.require_ai_image=true 면 모든 Scene 이 대본 내용대로 그린 AI 이미지여야 한다:
AI 이미지가 빠진 Scene 은 ai_retry_waits 만큼 쉬었다가 다시 그리고, 그래도 빠지면 MissingAIImages 로 단계를 실패시킨다
(자체 그래픽/어두운 화면으로 채워 업로드하지 않음). 이미 그린 Scene 은 existing 으로 넘겨 다시 그리지 않는다.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from app.assets.base import AssetProvider, AssetRequest
from app.assets.image_generator import (
    CloudflareImageProvider,
    GeminiImageProvider,
    HuggingFaceImageProvider,
    ProceduralImageProvider,
)
from app.assets.local import LocalAssetProvider
from app.assets.pexels import PexelsAdapter
from app.assets.pixabay import PixabayAdapter
from app.config.settings import Settings
from app.schemas import AssetInfo, Scene, SceneTiming
from app.utils.logger import get_logger
from app.utils.retry import NonRetryableError

logger = get_logger("assets")


def build_providers(settings: Settings, gemini_client=None, ai_fallback: bool = True) -> list[AssetProvider]:
    """ai_fallback=False 면 대체 AI 이미지(Cloudflare/Hugging Face)도 끈다 (offline 테스트에서 외부 AI 를 호출하지 않도록)."""
    ai = settings.assets.ai_image
    secrets = settings.secrets
    registry: dict[str, AssetProvider] = {
        "cf_image": CloudflareImageProvider(
            secrets.cloudflare_account_id, secrets.cloudflare_api_token, settings.assets.cf_image, ai.style,
            enabled=ai_fallback,
        ),
        "pexels": PexelsAdapter(settings.secrets.pexels_api_key, settings.assets),
        "pixabay": PixabayAdapter(settings.secrets.pixabay_api_key, settings.assets),
        "local": LocalAssetProvider(settings.paths.local_images, settings.paths.local_videos),
        "ai_image": GeminiImageProvider(gemini_client, [ai.model, *ai.fallback_models], ai.enabled, ai.style),
        "hf_image": HuggingFaceImageProvider(
            settings.secrets.hf_api_key, settings.assets.hf_image, ai.style, enabled=ai_fallback
        ),
        "procedural": ProceduralImageProvider(),
    }
    providers = [registry[name] for name in settings.assets.providers if name in registry]
    if not any(isinstance(p, ProceduralImageProvider) for p in providers):
        providers.append(registry["procedural"])  # 어떤 경우에도 영상 제작이 멈추지 않도록
    return providers


class MissingAIImages(Exception):
    """AI 이미지를 끝내 못 그린 Scene 이 있음 (재시도 가능: 나중에 다시 실행하면 빠진 Scene 만 그린다)."""


class AssetManager:
    def __init__(
        self,
        settings: Settings,
        providers: list[AssetProvider],
        used_ids: set[str] | None = None,
        require_ai: bool = False,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings
        self.providers = providers
        self.used_ids = set(used_ids or set())
        self.require_ai = require_ai
        self.sleep = sleep
        self.failure_counts: dict[str, int] = {}
        self.failed_providers: set[str] = set()   # 이번 라운드에서 건너뛰는 provider (일시 장애)
        self.hard_failed: set[str] = set()        # 인증/한도/크레딧 오류 → 이번 작업 내내 제외

    def _record_failure(self, name: str, limit: int = 2, hard: bool = False) -> None:
        """같은 provider 가 연속으로 오류를 내면(인증 오류, 서비스 장애) 이번 라운드에서는 건너뛴다."""
        self.failure_counts[name] = self.failure_counts.get(name, 0) + 1
        if hard:
            self.hard_failed.add(name)
        if self.failure_counts[name] >= limit:
            self.failed_providers.add(name)
            logger.warning("%s 오류 반복 → %s 제외", name, "이번 작업에서" if hard else "이번 라운드에서")

    def collect(
        self,
        scenes: list[Scene],
        dest_dir: Path,
        mood: str = "dark",
        timings: list[SceneTiming] | None = None,
        context: str = "",
        existing: list[AssetInfo] | None = None,
        on_progress: Callable[[list[AssetInfo]], None] | None = None,
    ) -> list[AssetInfo]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        timing_map = {t.scene_number: t for t in timings or []}
        active = [p for p in self.providers if p.available]
        ai_active = [p for p in active if p.generates_ai]
        logger.info("시각자료 Provider: %s", " → ".join(p.name for p in active))
        if self.require_ai and not ai_active:
            raise NonRetryableError(
                "AI 이미지 provider 가 하나도 없습니다 (CLOUDFLARE_ACCOUNT_ID/CLOUDFLARE_API_TOKEN 또는 HF_API_KEY 확인)."
                " assets.require_ai_image=true 라 자체 그래픽으로 대체하지 않습니다."
            )

        def duration_of(scene: Scene) -> float:
            timing = timing_map.get(scene.scene_number)
            return (timing.end - timing.start) if timing else max(scene.end - scene.start, 4.0)

        # 이전 실행에서 이미 그린 AI 이미지는 그대로 사용 (재시도 시 빠진 Scene 만 그림)
        done: dict[int, AssetInfo] = {
            a.scene_number: a for a in existing or []
            if a.generated_by_ai and Path(a.local_path).exists()
        }
        if done:
            logger.info("이전에 그린 AI 이미지 재사용: Scene %s", ", ".join(map(str, sorted(done))))

        def report() -> None:
            if on_progress:
                on_progress([done[n] for n in sorted(done)])

        assets: dict[int, AssetInfo] = dict(done)
        for scene in scenes:
            if scene.scene_number in assets:
                continue
            asset = self._collect_scene(scene, dest_dir, mood, duration_of(scene), active, context,
                                        allow_fallback=not self.require_ai)
            if asset:
                assets[scene.scene_number] = asset
                self.used_ids.add(asset.source_id)
                if asset.generated_by_ai:
                    done[scene.scene_number] = asset
                    report()
                logger.info("Scene %d 자료: %s %s (%s)", scene.scene_number, asset.source, asset.media_type, asset.query[:40])

        if self.require_ai:
            self._retry_missing(scenes, assets, dest_dir, mood, duration_of, ai_active, context, done, report)

        result = [assets[s.scene_number] for s in scenes]
        logger.info("Assets downloaded: %d개 (AI 이미지 %d개)", len(result), sum(a.generated_by_ai for a in result))
        return result

    def _retry_missing(self, scenes, assets, dest_dir, mood, duration_of, ai_active, context, done, report) -> None:
        """AI 이미지가 빠진 Scene 만 골라 쉬었다가 다시 그린다. 끝내 빠지면 MissingAIImages."""
        for round_no, wait in enumerate(self.settings.assets.ai_retry_waits, start=1):
            missing = [s for s in scenes if s.scene_number not in assets]
            if not missing:
                return
            usable = [p for p in ai_active if p.name not in self.hard_failed]
            if not usable:
                break
            logger.warning(
                "AI 이미지 없는 Scene %s → %.0f초 후 다시 그림 (재시도 %d/%d)",
                ", ".join(str(s.scene_number) for s in missing), wait, round_no, len(self.settings.assets.ai_retry_waits),
            )
            self.sleep(wait)
            # 일시 장애로 건너뛰던 provider 를 다시 사용 (인증/한도 오류는 계속 제외)
            self.failed_providers = set(self.hard_failed)
            self.failure_counts.clear()
            for scene in missing:
                for provider in usable:
                    provider.forget(scene.scene_number)
                asset = self._collect_scene(scene, dest_dir, mood, duration_of(scene), usable, context,
                                            allow_fallback=False)
                if asset:
                    assets[scene.scene_number] = done[scene.scene_number] = asset
                    report()
                    logger.info("Scene %d 자료: %s (재시도 %d 에서 성공)", scene.scene_number, asset.source, round_no)

        missing = [s.scene_number for s in scenes if s.scene_number not in assets]
        if missing:
            raise MissingAIImages(
                f"AI 이미지를 그리지 못한 Scene: {missing} (제외된 provider: {sorted(self.hard_failed) or '-'})."
                " 자체 그래픽으로 채우지 않고 중단합니다. 다시 실행하면 그린 Scene 은 재사용하고 빠진 Scene 만 그립니다."
            )

    def _collect_scene(
        self, scene: Scene, dest_dir: Path, mood: str, duration: float, providers: list[AssetProvider],
        context: str = "", allow_fallback: bool = True,
    ) -> AssetInfo | None:
        preferred = scene.visual_type if not self.settings.assets.prefer_video or scene.visual_type == "image" else "video"
        media_order = [preferred, "image" if preferred == "video" else "video"]
        queries = [q for q in [scene.visual_search_query, *scene.alt_search_queries] if q and q.strip()]

        for provider in providers:
            if provider.name == "procedural" or provider.name in self.failed_providers:
                continue
            if not allow_fallback and not provider.generates_ai:
                continue  # AI 이미지 필수: 스톡/로컬 자료는 대본 내용과 맞지 않으므로 쓰지 않음
            for query in queries:
                for media_type in media_order:
                    if media_type not in provider.supports:
                        continue
                    request = AssetRequest(
                        scene_number=scene.scene_number, query=query.strip(), media_type=media_type,
                        min_duration=duration, visual_prompt=scene.visual_prompt or scene.visual, mood=mood,
                        exclude_ids=self.used_ids, context=context,
                    )
                    try:
                        asset = provider.fetch(request, dest_dir)
                    except Exception as exc:
                        logger.warning("%s 자료 수집 실패 (scene %d, '%s'): %s", provider.name, scene.scene_number, query, exc)
                        # 인증/한도/권한 오류는 다음 Scene 에서도 같으므로 바로 제외
                        hard = isinstance(exc, NonRetryableError)
                        self._record_failure(provider.name, limit=1 if hard else 2, hard=hard)
                        if provider.name in self.failed_providers:
                            break
                        continue
                    if asset:
                        # 제외 기준은 "연속" 오류 → 성공하면 다시 0 부터 센다 (간헐적 타임아웃으로 제외되지 않도록)
                        self.failure_counts.pop(provider.name, None)
                        return asset
                if provider.name in self.failed_providers:
                    break

        if not allow_fallback:
            return None
        fallback = ProceduralImageProvider().fetch(
            AssetRequest(scene.scene_number, queries[0] if queries else "mystery", "image", duration, mood=mood), dest_dir
        )
        assert fallback is not None
        return fallback
