"""Pexels API Adapter (https://www.pexels.com/api/). 세로(portrait) 자료를 우선 검색한다."""

from __future__ import annotations

from pathlib import Path

import requests

from app.assets.base import AssetProvider, AssetRequest
from app.config.settings import AssetsConfig
from app.schemas import AssetInfo
from app.utils.files import download_file, now_iso
from app.utils.logger import get_logger
from app.utils.retry import retry_call

logger = get_logger("pexels")

LICENSE = "Pexels License (https://www.pexels.com/license/)"


def pick_video_file(files: list[dict], min_short_side: int) -> dict | None:
    """세로형 + 적당한 해상도(1080~2160 높이)를 우선, 너무 큰 4K 이상은 피한다."""
    usable = [f for f in files if f.get("link") and f.get("width") and f.get("height") and "mp4" in (f.get("file_type") or "mp4")]
    if not usable:
        return None

    def score(f: dict) -> tuple:
        w, h = f["width"], f["height"]
        portrait = h >= w
        short = min(w, h)
        too_big = max(w, h) > 2600
        return (portrait, short >= min_short_side, not too_big, -abs(h - 1920) if portrait else short)

    return max(usable, key=score)


class PexelsAdapter(AssetProvider):
    name = "pexels"

    def __init__(self, api_key: str, config: AssetsConfig):
        self.api_key = api_key
        self.config = config
        self.session = requests.Session()
        self.session.headers["Authorization"] = api_key

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _get(self, url: str, params: dict) -> dict:
        def _call() -> dict:
            resp = self.session.get(url, params=params, timeout=self.config.request_timeout)
            resp.raise_for_status()
            return resp.json()

        return retry_call(_call, step="Pexels", max_attempts=3, delays=((2, 4), (5, 10)))

    def fetch(self, request: AssetRequest, dest_dir: Path) -> AssetInfo | None:
        if request.media_type == "video":
            return self._fetch_video(request, dest_dir)
        return self._fetch_photo(request, dest_dir)

    def _fetch_video(self, request: AssetRequest, dest_dir: Path) -> AssetInfo | None:
        data = self._get(
            "https://api.pexels.com/videos/search",
            {"query": request.query, "orientation": "portrait", "per_page": self.config.per_page},
        )
        videos = data.get("videos", [])
        # 장면 길이보다 긴 영상 우선 (짧으면 반복 재생)
        videos.sort(key=lambda v: (v.get("duration", 0) >= request.min_duration), reverse=True)
        for video in videos:
            vid = str(video.get("id"))
            if vid in request.exclude_ids:
                continue
            chosen = pick_video_file(video.get("video_files", []), self.config.min_short_side)
            if not chosen:
                continue
            dest = dest_dir / f"scene_{request.scene_number:02d}_pexels_{vid}.mp4"
            download_file(chosen["link"], dest, timeout=self.config.request_timeout * 4, max_mb=self.config.max_download_mb)
            user = video.get("user") or {}
            return AssetInfo(
                scene_number=request.scene_number, media_type="video", local_path=str(dest), source="pexels",
                source_id=vid, source_url=video.get("url", ""), author=user.get("name", ""), author_url=user.get("url", ""),
                license=LICENSE, downloaded_at=now_iso(), query=request.query,
                width=chosen["width"], height=chosen["height"], duration=float(video.get("duration") or 0),
            )
        return None

    def _fetch_photo(self, request: AssetRequest, dest_dir: Path) -> AssetInfo | None:
        data = self._get(
            "https://api.pexels.com/v1/search",
            {"query": request.query, "orientation": "portrait", "per_page": self.config.per_page},
        )
        for photo in data.get("photos", []):
            pid = str(photo.get("id"))
            if pid in request.exclude_ids:
                continue
            src = photo.get("src") or {}
            url = src.get("large2x") or src.get("portrait") or src.get("original")
            if not url:
                continue
            dest = dest_dir / f"scene_{request.scene_number:02d}_pexels_{pid}.jpg"
            download_file(url, dest, timeout=self.config.request_timeout * 2, max_mb=self.config.max_download_mb)
            return AssetInfo(
                scene_number=request.scene_number, media_type="image", local_path=str(dest), source="pexels",
                source_id=pid, source_url=photo.get("url", ""), author=photo.get("photographer", ""),
                author_url=photo.get("photographer_url", ""), license=LICENSE, downloaded_at=now_iso(),
                query=request.query, width=photo.get("width", 0), height=photo.get("height", 0),
            )
        return None
