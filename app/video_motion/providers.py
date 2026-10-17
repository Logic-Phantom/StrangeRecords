"""Image-to-Video Provider Adapter.

ZhipuCogVideoFlashProvider (智谱 BigModel CogVideoX-Flash)
- 공식 문서(2026-10-17 확인): "智谱推出的免费视频生成模型" (무료 영상 생성 모델), 이미지(URL/base64) + 프롬프트 입력,
  비동기 작업 → 결과 조회. https://docs.bigmodel.cn/cn/guide/models/free/cogvideox-flash
- 미확인(UNKNOWN): 무료 요청 한도(일/분), 한도 초과 시 동작, 해외 전화번호 가입 가능 여부, 결과물 상업적 이용 조건,
  영상 길이, 영상 URL 보관 기간, 중국 외 서버(GitHub Actions) 호출 허용 여부
  → free_status = UNVERIFIED: FreeQuotaGuard 가 호출을 막는다. 위 항목을 사람이 공식 자료로 확인한 뒤에만
    코드에서 VERIFIED_FREE 로 바꾼다 (설정 파일로는 바꿀 수 없음).
"""

from __future__ import annotations

import base64
import io
import time
from typing import Callable

import requests
from PIL import Image

from app.config.settings import MotionProviderConfig
from app.video_motion.base import (
    ErrorKind,
    FreeStatus,
    ImageToVideoProvider,
    MotionProviderError,
    MotionRequest,
    MotionResult,
    classify_http_error,
)
from app.video_motion.result_validator import looks_like_video

MAX_DOWNLOAD_MB = 80


def _error_body(resp: requests.Response) -> str:
    try:
        data = resp.json()
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            return f"{err.get('code', '')} {err.get('message', '')}".strip()
        return str(data)[:300]
    except ValueError:
        return (resp.text or "")[:300]


def _raise_for(resp: requests.Response, what: str) -> None:
    if resp.status_code < 400:
        return
    body = _error_body(resp)
    kind = classify_http_error(resp.status_code, body, resp.headers.get("Retry-After"))
    # 응답 본문은 오류 코드/메시지만 기록 (키/토큰은 요청 헤더에만 있고 로그에 남기지 않음)
    raise MotionProviderError(kind, f"{what} HTTP {resp.status_code}: {body[:200]}", resp.status_code)


def image_data_uri(path, max_side: int = 1344, max_bytes: int = 4_500_000) -> str:
    """원본 이미지는 그대로 두고 메모리에서만 JPEG 로 줄여 base64 data URI 로 만든다 (5MB 제한)."""
    with Image.open(path) as img:
        img = img.convert("RGB")
        img.thumbnail((max_side, max_side))
        quality = 92
        while True:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            if buf.tell() <= max_bytes or quality <= 60:
                break
            quality -= 8
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


class ZhipuCogVideoFlashProvider(ImageToVideoProvider):
    name = "zhipu_cogvideox_flash"
    free_status = FreeStatus.UNVERIFIED
    checked_at = "2026-10-17"
    official_urls = (
        "https://docs.bigmodel.cn/cn/guide/models/free/cogvideox-flash",
        "https://docs.bigmodel.cn/api-reference/模型-api/视频生成异步",
        "https://docs.bigmodel.cn/api-reference/模型-api/查询异步结果",
    )
    official_free_requests_per_day = None   # 공식 수치 미확인
    max_clip_seconds = None                 # flash 는 duration 파라미터 없음 (길이 미확인)
    note = "무료 모델 표기만 확인. 한도·초과 시 동작·상업적 이용·해외 가입 미확인 → 호출 안 함"

    SUBMIT = "https://open.bigmodel.cn/api/paas/v4/videos/generations"
    RESULT = "https://open.bigmodel.cn/api/paas/v4/async-result/{id}"
    DEFAULT_MODEL = "cogvideox-flash"

    def __init__(self, api_key: str, config: MotionProviderConfig, session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.api_key = api_key
        self.config = config
        self.session = session or requests.Session()
        self.sleep = sleep
        self.clock = clock

    @property
    def model(self) -> str:
        return self.config.model or self.DEFAULT_MODEL

    @property
    def configured(self) -> bool:
        return bool(self.config.enabled and self.api_key and self.model)

    def cache_params(self) -> dict:
        params = {"size": "1080x1920", "fps": 30, "quality": "speed", "with_audio": False}
        params.update(self.config.params or {})
        return params

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _request(self, method: str, url: str, what: str, **kwargs) -> requests.Response:
        try:
            resp = self.session.request(method, url, headers=self._headers(), timeout=self.config.timeout, **kwargs)
        except requests.RequestException as exc:
            raise MotionProviderError(ErrorKind.TRANSIENT, f"{what} 네트워크 오류: {type(exc).__name__}") from exc
        _raise_for(resp, what)
        return resp

    def generate(self, request: MotionRequest) -> MotionResult:
        payload = {"model": self.model, "prompt": request.prompt[:500], "image_url": image_data_uri(request.image_path),
                   **self.cache_params()}
        resp = self._request("POST", self.SUBMIT, "CogVideoX 요청", json=payload)
        data = resp.json()
        task_id = data.get("id")
        if not task_id:
            raise MotionProviderError(ErrorKind.INVALID_RESULT, f"CogVideoX 작업 ID 없음: {str(data)[:200]}")
        status = data.get("task_status")
        started = self.clock()
        while True:
            if status == "FAIL":
                raise MotionProviderError(ErrorKind.INVALID_RESULT, "CogVideoX 생성 실패 (task_status=FAIL)")
            if status == "SUCCESS":
                break
            if self.clock() - started > self.config.max_poll_seconds:
                # 비동기 작업은 제한된 시간 안에서만 확인한다 (무한 대기 금지)
                raise MotionProviderError(ErrorKind.TRANSIENT, f"CogVideoX 결과 대기 시간 초과 ({self.config.max_poll_seconds}s)")
            self.sleep(self.config.poll_interval)
            result = self._request("GET", self.RESULT.format(id=task_id), "CogVideoX 결과 조회").json()
            status = result.get("task_status")
            data = result
        videos = data.get("video_result") or []
        url = (videos[0] or {}).get("url") if videos else None
        if not url:
            raise MotionProviderError(ErrorKind.INVALID_RESULT, "CogVideoX 결과에 영상 URL 없음")
        return MotionResult(self._download(url), self.model, extra={"task_id": task_id})

    def _download(self, url: str) -> bytes:
        try:
            with self.session.get(url, stream=True, timeout=self.config.timeout) as resp:
                _raise_for(resp, "CogVideoX 영상 다운로드")
                chunks, size = [], 0
                for chunk in resp.iter_content(chunk_size=256 * 1024):
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_MB * 1024 * 1024:
                        raise MotionProviderError(ErrorKind.INVALID_RESULT, f"영상 크기 제한 초과({MAX_DOWNLOAD_MB}MB)")
                    chunks.append(chunk)
        except requests.RequestException as exc:
            raise MotionProviderError(ErrorKind.TRANSIENT, f"영상 다운로드 오류: {type(exc).__name__}") from exc
        data = b"".join(chunks)
        if not looks_like_video(data):
            raise MotionProviderError(ErrorKind.INVALID_RESULT, "다운로드한 파일이 영상이 아님")
        return data
