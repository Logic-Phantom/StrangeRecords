"""설정(video_motion.providers) → Image-to-Video Provider 목록 + 후보 서비스 조사 기록.

- 조사 기록(RESEARCH)은 2026-10-17 공식 문서 기준이다. 유료/미검증 후보는 Adapter 를 만들지 않거나(유료),
  만들어도 free_status 가 verified_free 가 아니라서 FreeQuotaGuard 가 호출을 막는다.
- 설정에 유료 provider 이름을 넣어도 목록에 들어가지 않는다 (경고만 남김).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config.settings import Settings
from app.utils.logger import get_logger
from app.video_motion.base import FreeStatus, ImageToVideoProvider
from app.video_motion.providers import ZhipuCogVideoFlashProvider

logger = get_logger("motion_providers")


@dataclass(frozen=True)
class ProviderResearch:
    name: str
    service: str
    status: str            # verified_free | unverified | paid | unavailable
    summary: str
    urls: tuple[str, ...]


CHECKED_AT = "2026-10-17"
RESEARCH: tuple[ProviderResearch, ...] = (
    ProviderResearch(
        "zhipu_cogvideox_flash", "智谱 BigModel CogVideoX-Flash", FreeStatus.UNVERIFIED,
        "무료 영상 생성 모델로 표기, 이미지+프롬프트 비동기 API. 무료 한도·초과 시 동작·상업적 이용·해외 가입·영상 길이 UNKNOWN",
        ("https://docs.bigmodel.cn/cn/guide/models/free/cogvideox-flash",
         "https://docs.bigmodel.cn/api-reference/模型-api/视频生成异步"),
    ),
    ProviderResearch(
        "huggingface", "Hugging Face Inference Providers (image_to_video)", FreeStatus.PAID,
        "무료 사용자 월 크레딧 None, 크레딧 구매 후 종량제. 이미 402 Payment Required 확인",
        ("https://huggingface.co/docs/inference-providers/pricing",),
    ),
    ProviderResearch(
        "cloudflare", "Cloudflare Workers AI", "unavailable",
        "모델 카탈로그에 image-to-video/영상 생성 모델 없음 (무료 한도 초과 시 오류로 거절되는 구조는 확인)",
        ("https://developers.cloudflare.com/workers-ai/models/",
         "https://developers.cloudflare.com/workers-ai/platform/pricing/"),
    ),
    ProviderResearch(
        "gemini_veo", "Google Gemini API Veo 3.1", FreeStatus.PAID,
        "무료 티어 Not available (초당 $0.05~$0.40)", ("https://ai.google.dev/gemini-api/docs/pricing",),
    ),
    ProviderResearch(
        "alibaba_wan", "Alibaba Model Studio Wan I2V", FreeStatus.PAID,
        "모델당 1회성 무료 50초(90일), 활성화에 결제 정보 등록 필요, 'Free Quota Only' 기본 꺼짐",
        ("https://www.alibabacloud.com/help/en/model-studio/new-free-quota",),
    ),
    ProviderResearch(
        "others", "Replicate / fal.ai / Runway / Luma / Pollinations / SiliconFlow / Together", FreeStatus.PAID,
        "공식 무료 사용량 없음 또는 금액 미기재 (결제/크레딧 구매 필요)",
        ("https://replicate.com/pricing", "https://fal.ai/pricing"),
    ),
)

_REGISTRY = {"zhipu_cogvideox_flash": ZhipuCogVideoFlashProvider}
_PAID = {r.name for r in RESEARCH if r.status == FreeStatus.PAID}


def build_motion_providers(settings: Settings) -> list[ImageToVideoProvider]:
    providers: list[ImageToVideoProvider] = []
    for cfg in settings.video_motion.providers:
        if cfg.name in _PAID:
            logger.warning("Image-to-Video provider '%s' 는 유료라 사용하지 않습니다 (비용 안전 정책)", cfg.name)
            continue
        cls = _REGISTRY.get(cfg.name)
        if cls is None:
            logger.warning("알 수 없는 Image-to-Video provider '%s' → 무시", cfg.name)
            continue
        if cls is ZhipuCogVideoFlashProvider:
            providers.append(ZhipuCogVideoFlashProvider(settings.secrets.zhipu_api_key, cfg))
    return providers
