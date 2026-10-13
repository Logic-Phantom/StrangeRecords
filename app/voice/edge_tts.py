"""Edge TTS (무료, API Key 불필요) 음성 Provider.

2026-10 부터 Microsoft 가 기존 합성 주소(speech.platform.bing.com)를 403 으로 막아
edge-tts 7.2.8(최신)이 모든 요청에 실패한다 (rany2/edge-tts#490).
Edge 155 가 쓰는 api.msedgeservices.com 은 같은 토큰을 받아 주므로 voice.endpoints 순서대로 시도하고,
403(접속 거부)이면 다음 주소로 넘어간다. 성공한 주소는 이번 실행 동안 계속 사용한다.
새 주소는 연속 요청에 429 를 돌려주므로(실측: 2초 이상 간격이면 통과) 요청 사이에 voice.min_interval 만큼 쉬고,
429 를 받으면 voice.rate_limit_waits 대로 기다렸다가 다시 시도한다.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import aiohttp
import edge_tts
import edge_tts.communicate
from edge_tts.constants import TRUSTED_CLIENT_TOKEN

from app.config.settings import RetryConfig, VoiceConfig
from app.utils.logger import get_logger
from app.utils.retry import retry_call
from app.voice.base import VoiceProvider

logger = get_logger("voice")

ENDPOINT_URLS = {
    "msedgeservices": (
        "wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1"
        f"?Ocp-Apim-Subscription-Key={TRUSTED_CLIENT_TOKEN}"
    ),
    "bing": (
        "wss://speech.platform.bing.com/consumer/speech/synthesize/readaloud/edge/v1"
        f"?TrustedClientToken={TRUSTED_CLIENT_TOKEN}"
    ),
}


def _status(exc: BaseException) -> int | None:
    return exc.status if isinstance(exc, aiohttp.ClientResponseError) else None


def _is_forbidden(exc: BaseException) -> bool:
    return isinstance(exc, aiohttp.WSServerHandshakeError) and exc.status in (401, 403)


class EdgeTTSProvider(VoiceProvider):
    name = "edge_tts"

    def __init__(self, config: VoiceConfig, retry: RetryConfig | None = None, sleep=time.sleep):
        self.config = config
        self.retry = retry or RetryConfig()
        self.endpoints = [ENDPOINT_URLS.get(e, e) for e in config.endpoints] or [edge_tts.communicate.WSS_URL]
        self._sleep = sleep
        self._last_request = 0.0

    async def _save(self, text: str, output_path: Path, rate: str, pitch: str) -> None:
        communicate = edge_tts.Communicate(text, self.config.voice, rate=rate, pitch=pitch)
        await communicate.save(str(output_path))

    def _request(self, text: str, output_path: Path, rate: str, pitch: str) -> None:
        """요청 간격(min_interval)을 지키고, 429(요청 과다)면 rate_limit_waits 대로 기다렸다가 다시 보낸다."""
        waits = list(self.config.rate_limit_waits)
        while True:
            gap = self.config.min_interval - (time.monotonic() - self._last_request)
            if gap > 0:
                self._sleep(gap)
            self._last_request = time.monotonic()
            try:
                asyncio.run(self._save(text, output_path, rate, pitch))
                return
            except Exception as exc:
                if _status(exc) != 429 or not waits:
                    raise
                wait = waits.pop(0)
                logger.warning("Edge TTS 요청 과다(429) → %.0f초 대기 후 다시 시도", wait)
                self._sleep(wait)

    def _synthesize_once(self, text: str, output_path: Path, rate: str, pitch: str) -> None:
        """endpoints 를 순서대로 시도. 403 이면 다음 주소, 그 외 오류는 그대로 올려 재시도에 맡긴다."""
        last: BaseException | None = None
        for i, url in enumerate(self.endpoints):
            # edge_tts.communicate 는 모듈 상수 WSS_URL 로 접속 주소를 만든다
            edge_tts.communicate.WSS_URL = url
            try:
                self._request(text, output_path, rate, pitch)
            except Exception as exc:
                if not _is_forbidden(exc) or i == len(self.endpoints) - 1:
                    raise
                logger.warning("Edge TTS 주소 거부(%s) → 다음 주소로 전환: %s", getattr(exc, "status", "?"), url.split("?")[0])
                last = exc
                continue
            if i > 0:
                # 성공한 주소를 맨 앞으로 옮겨 다음 Scene 부터 바로 사용
                self.endpoints.insert(0, self.endpoints.pop(i))
            return
        if last:
            raise last

    def synthesize(self, text: str, output_path: Path, *, rate: str | None = None, pitch: str | None = None) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        rate = rate or self.config.rate
        pitch = pitch or self.config.pitch

        def _call() -> Path:
            self._synthesize_once(text, output_path, rate, pitch)
            if not output_path.exists() or output_path.stat().st_size < 1000:
                raise RuntimeError("Edge TTS 결과 파일이 비어 있음")
            return output_path

        return retry_call(_call, step="Edge TTS", max_attempts=self.retry.max_attempts, delays=self.retry.delays)


def create_voice_provider(config: VoiceConfig, retry: RetryConfig | None = None) -> VoiceProvider:
    if config.provider == "edge_tts":
        return EdgeTTSProvider(config, retry)
    raise ValueError(f"지원하지 않는 voice provider: {config.provider}")
