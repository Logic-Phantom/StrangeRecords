"""Edge TTS (무료, API Key 불필요) 음성 Provider."""

from __future__ import annotations

import asyncio
from pathlib import Path

import edge_tts

from app.config.settings import RetryConfig, VoiceConfig
from app.utils.logger import get_logger
from app.utils.retry import retry_call
from app.voice.base import VoiceProvider

logger = get_logger("voice")


class EdgeTTSProvider(VoiceProvider):
    name = "edge_tts"

    def __init__(self, config: VoiceConfig, retry: RetryConfig | None = None):
        self.config = config
        self.retry = retry or RetryConfig()

    async def _save(self, text: str, output_path: Path, rate: str, pitch: str) -> None:
        communicate = edge_tts.Communicate(text, self.config.voice, rate=rate, pitch=pitch)
        await communicate.save(str(output_path))

    def synthesize(self, text: str, output_path: Path, *, rate: str | None = None, pitch: str | None = None) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        rate = rate or self.config.rate
        pitch = pitch or self.config.pitch

        def _call() -> Path:
            asyncio.run(self._save(text, output_path, rate, pitch))
            if not output_path.exists() or output_path.stat().st_size < 1000:
                raise RuntimeError("Edge TTS 결과 파일이 비어 있음")
            return output_path

        return retry_call(_call, step="Edge TTS", max_attempts=self.retry.max_attempts, delays=self.retry.delays)


def create_voice_provider(config: VoiceConfig, retry: RetryConfig | None = None) -> VoiceProvider:
    if config.provider == "edge_tts":
        return EdgeTTSProvider(config, retry)
    raise ValueError(f"지원하지 않는 voice provider: {config.provider}")
