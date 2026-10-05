"""음성 Provider Adapter 인터페이스."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class VoiceProvider(ABC):
    name: str = "base"

    @abstractmethod
    def synthesize(self, text: str, output_path: Path, *, rate: str | None = None, pitch: str | None = None) -> Path:
        """text 를 음성 파일로 저장하고 경로를 반환한다."""


def parse_percent(value: str) -> int:
    value = value.strip().rstrip("%")
    return int(value) if value else 0


def format_percent(value: int) -> str:
    return f"{value:+d}%"
