"""Edge TTS 접속 주소 전환 테스트 (실제 Microsoft 서버는 호출하지 않는다)."""

from __future__ import annotations

import aiohttp
import edge_tts.communicate
import pytest
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from app.voice import edge_tts as voice


def _handshake_error(status: int = 403) -> aiohttp.WSServerHandshakeError:
    url = URL("wss://example.invalid/tts")
    info = aiohttp.RequestInfo(url=url, method="GET", headers=CIMultiDictProxy(CIMultiDict()), real_url=url)
    return aiohttp.WSServerHandshakeError(request_info=info, history=(), status=status, message="Invalid response status")


def _provider(settings, monkeypatch, fail_hosts: set[str], calls: list[str]):
    settings.voice.endpoints = ["bing", "msedgeservices"]
    settings.voice.min_interval = 0
    settings.voice.rate_limit_waits = [0, 0]
    provider = voice.EdgeTTSProvider(settings.voice, settings.retry, sleep=lambda s: None)

    async def fake_save(text, output_path, rate, pitch):
        url = edge_tts.communicate.WSS_URL
        calls.append(url)
        if any(host in url for host in fail_hosts):
            raise _handshake_error()
        output_path.write_bytes(b"\0" * 2000)

    monkeypatch.setattr(provider, "_save", fake_save)
    return provider


def test_forbidden_endpoint_switches_and_sticks(settings, monkeypatch, tmp_path):
    calls: list[str] = []
    provider = _provider(settings, monkeypatch, {"speech.platform.bing.com"}, calls)
    provider.synthesize("하나", tmp_path / "1.mp3")
    provider.synthesize("둘", tmp_path / "2.mp3")
    assert "speech.platform.bing.com" in calls[0] and "api.msedgeservices.com" in calls[1]
    assert len(calls) == 3 and "api.msedgeservices.com" in calls[2]  # 성공한 주소를 다음부터 바로 사용


def test_all_endpoints_forbidden_raises(settings, monkeypatch, tmp_path):
    calls: list[str] = []
    provider = _provider(settings, monkeypatch, {"bing.com", "msedgeservices.com"}, calls)
    with pytest.raises(Exception):
        provider.synthesize("하나", tmp_path / "1.mp3")
    assert len(calls) == 2 * settings.retry.max_attempts


def test_rate_limited_request_waits_and_retries(settings, monkeypatch, tmp_path):
    settings.voice.endpoints = ["msedgeservices"]
    settings.voice.min_interval = 0
    settings.voice.rate_limit_waits = [5, 10]
    slept: list[float] = []
    provider = voice.EdgeTTSProvider(settings.voice, settings.retry, sleep=slept.append)
    responses = [429, 429, None]

    async def fake_save(text, output_path, rate, pitch):
        status = responses.pop(0)
        if status:
            raise _handshake_error(status)
        output_path.write_bytes(b"\0" * 2000)

    monkeypatch.setattr(provider, "_save", fake_save)
    provider.synthesize("하나", tmp_path / "1.mp3")
    assert slept == [5, 10] and not responses
