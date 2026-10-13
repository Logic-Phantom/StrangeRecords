"""시각자료 Provider Adapter 인터페이스."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from app.schemas import AssetInfo


@dataclass
class AssetRequest:
    scene_number: int
    query: str
    media_type: str            # "video" | "image"
    min_duration: float        # 필요한 최소 길이(초) - 영상일 때
    visual_prompt: str = ""
    mood: str = "dark"
    context: str = ""          # 영상 전체 주제/줄거리 (AI 이미지가 장면마다 같은 이야기를 그리도록)
    exclude_ids: set[str] = field(default_factory=set)


class AssetProvider(ABC):
    name: str = "base"
    supports: tuple[str, ...] = ("video", "image")
    generates_ai: bool = False   # 장면 내용대로 이미지를 새로 그리는 AI provider 인가

    @property
    def available(self) -> bool:
        return True

    def forget(self, scene_number: int) -> None:
        """이 Scene 을 다시 시도할 수 있게 한다 (AI provider 는 Scene 당 한 번만 시도하므로 재시도 라운드에서 사용)."""
        tried = getattr(self, "_tried", None)
        if isinstance(tried, set):
            tried.discard(scene_number)

    @abstractmethod
    def fetch(self, request: AssetRequest, dest_dir: Path) -> AssetInfo | None:
        """조건에 맞는 자료를 dest_dir 에 저장하고 출처 정보를 반환한다. 없으면 None."""
