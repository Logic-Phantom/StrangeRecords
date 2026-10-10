"""Scene 별 시각자료 수집 (config.assets.providers 순서, 기본: Gemini 이미지 → HF 이미지 → Pexels → Pixabay → 로컬 → 자체 생성)."""

from __future__ import annotations

from pathlib import Path

from app.assets.base import AssetProvider, AssetRequest
from app.assets.image_generator import GeminiImageProvider, HuggingFaceImageProvider, ProceduralImageProvider
from app.assets.local import LocalAssetProvider
from app.assets.pexels import PexelsAdapter
from app.assets.pixabay import PixabayAdapter
from app.config.settings import Settings
from app.schemas import AssetInfo, Scene, SceneTiming
from app.utils.logger import get_logger
from app.utils.retry import NonRetryableError

logger = get_logger("assets")


def build_providers(settings: Settings, gemini_client=None, ai_fallback: bool = True) -> list[AssetProvider]:
    """ai_fallback=False 면 Hugging Face 이미지도 끈다 (offline 테스트에서 외부 AI 크레딧을 쓰지 않도록)."""
    ai = settings.assets.ai_image
    registry: dict[str, AssetProvider] = {
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


class AssetManager:
    def __init__(self, settings: Settings, providers: list[AssetProvider], used_ids: set[str] | None = None):
        self.settings = settings
        self.providers = providers
        self.used_ids = set(used_ids or set())
        self.failure_counts: dict[str, int] = {}
        self.failed_providers: set[str] = set()

    def _record_failure(self, name: str, limit: int = 2) -> None:
        """같은 provider 가 연속으로 오류를 내면(인증 오류, 서비스 장애) 이번 작업에서는 건너뛴다."""
        self.failure_counts[name] = self.failure_counts.get(name, 0) + 1
        if self.failure_counts[name] >= limit:
            self.failed_providers.add(name)
            logger.warning("%s 오류 반복 → 이번 작업에서 제외", name)

    def collect(
        self,
        scenes: list[Scene],
        dest_dir: Path,
        mood: str = "dark",
        timings: list[SceneTiming] | None = None,
        context: str = "",
    ) -> list[AssetInfo]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        timing_map = {t.scene_number: t for t in timings or []}
        active = [p for p in self.providers if p.available]
        logger.info("시각자료 Provider: %s", " → ".join(p.name for p in active))

        assets: list[AssetInfo] = []
        for scene in scenes:
            timing = timing_map.get(scene.scene_number)
            duration = (timing.end - timing.start) if timing else max(scene.end - scene.start, 4.0)
            asset = self._collect_scene(scene, dest_dir, mood, duration, active, context)
            assets.append(asset)
            self.used_ids.add(asset.source_id)
            logger.info(
                "Scene %d 자료: %s %s (%s)", scene.scene_number, asset.source, asset.media_type, asset.query[:40]
            )
        logger.info("Assets downloaded: %d개", len(assets))
        return assets

    def _collect_scene(
        self, scene: Scene, dest_dir: Path, mood: str, duration: float, providers: list[AssetProvider],
        context: str = "",
    ) -> AssetInfo:
        preferred = scene.visual_type if not self.settings.assets.prefer_video or scene.visual_type == "image" else "video"
        media_order = [preferred, "image" if preferred == "video" else "video"]
        queries = [q for q in [scene.visual_search_query, *scene.alt_search_queries] if q and q.strip()]

        for provider in providers:
            if provider.name == "procedural" or provider.name in self.failed_providers:
                continue
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
                        self._record_failure(provider.name, limit=1 if isinstance(exc, NonRetryableError) else 2)
                        if provider.name in self.failed_providers:
                            break
                        continue
                    if asset:
                        return asset
                if provider.name in self.failed_providers:
                    break

        fallback = ProceduralImageProvider().fetch(
            AssetRequest(scene.scene_number, queries[0] if queries else "mystery", "image", duration, mood=mood), dest_dir
        )
        assert fallback is not None
        return fallback
