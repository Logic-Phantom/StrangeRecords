"""AI 영상 캐시 — 같은 이미지·프롬프트·provider·모델·길이·출력 설정이면 API 를 다시 호출하지 않는다.

파일: output/motion_cache/<cache_key>.mp4 (원본 이미지와 별도, 원본은 덮어쓰지 않음)
GitHub Actions 는 실행마다 새 서버라 파일 캐시는 같은 실행 안의 재시도(실패 단계부터 재실행)에서만 남는다.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

CACHE_VERSION = 1


def file_sha256(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def cache_key(image_sha256: str, prompt: str, provider: str, model: str, clip_seconds: float,
              width: int, height: int, fps: int, params: dict | None = None) -> str:
    payload = {
        "v": CACHE_VERSION, "image": image_sha256, "prompt": prompt, "provider": provider, "model": model,
        "seconds": round(float(clip_seconds), 2), "size": [int(width), int(height)], "fps": int(fps),
        "params": params or {},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


class MotionCache:
    def __init__(self, directory: Path, enabled: bool = True):
        self.directory = Path(directory)
        self.enabled = enabled

    def path_for(self, key: str, suffix: str = ".mp4") -> Path:
        return self.directory / f"{key}{suffix}"

    def lookup(self, key: str) -> Path | None:
        if not self.enabled:
            return None
        for suffix in (".mp4", ".webm", ".mov"):
            path = self.path_for(key, suffix)
            if path.exists() and path.stat().st_size > 0:
                return path
        return None

    def store(self, key: str, data: bytes, suffix: str = ".mp4") -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(key, suffix)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)
        return path

    def discard(self, path: Path) -> None:
        """검증에 실패한 파일만 지운다 (정상 영상은 지우지 않음)."""
        if path.parent.resolve() == self.directory.resolve():
            path.unlink(missing_ok=True)
