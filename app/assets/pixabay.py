"""Pixabay API Adapter (https://pixabay.com/api/docs/). 이미지 핫링크 금지 → 반드시 다운로드해서 사용."""

from __future__ import annotations

from pathlib import Path

import requests

from app.assets.base import AssetProvider, AssetRequest
from app.config.settings import AssetsConfig
from app.schemas import AssetInfo
from app.utils.files import download_file, now_iso
from app.utils.logger import get_logger
from app.utils.retry import retry_call

logger = get_logger("pixabay")

LICENSE = "Pixabay Content License (https://pixabay.com/service/license-summary/)"


def pick_pixabay_video(videos: dict, min_short_side: int) -> dict | None:
    options = [v for v in (videos.get(k) for k in ("large", "medium", "small", "tiny")) if v and v.get("url")]
    if not options:
        return None
    good = [v for v in options if min(v.get("width", 0), v.get("height", 0)) >= min_short_side and max(v.get("width", 0), v.get("height", 0)) <= 2600]
    return (good or options)[0]


class PixabayAdapter(AssetProvider):
    name = "pixabay"

    def __init__(self, api_key: str, config: AssetsConfig):
        self.api_key = api_key
        self.config = config
        self.session = requests.Session()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _get(self, url: str, params: dict) -> dict:
        def _call() -> dict:
            resp = self.session.get(url, params={"key": self.api_key, **params}, timeout=self.config.request_timeout)
            resp.raise_for_status()
            return resp.json()

        return retry_call(_call, step="Pixabay", max_attempts=3, delays=((2, 4), (5, 10)))

    def fetch(self, request: AssetRequest, dest_dir: Path) -> AssetInfo | None:
        if request.media_type == "video":
            return self._fetch_video(request, dest_dir)
        return self._fetch_image(request, dest_dir)

    def _fetch_video(self, request: AssetRequest, dest_dir: Path) -> AssetInfo | None:
        data = self._get(
            "https://pixabay.com/api/videos/",
            {"q": request.query[:100], "per_page": max(3, self.config.per_page), "safesearch": "true"},
        )
        hits = data.get("hits", [])
        # 세로 영상 우선, 다음으로 길이 충분한 영상
        def rank(hit: dict) -> tuple:
            large = (hit.get("videos") or {}).get("large") or {}
            return (large.get("height", 0) >= large.get("width", 1), hit.get("duration", 0) >= request.min_duration)

        for hit in sorted(hits, key=rank, reverse=True):
            hid = str(hit.get("id"))
            if hid in request.exclude_ids:
                continue
            chosen = pick_pixabay_video(hit.get("videos") or {}, self.config.min_short_side)
            if not chosen:
                continue
            dest = dest_dir / f"scene_{request.scene_number:02d}_pixabay_{hid}.mp4"
            download_file(chosen["url"], dest, timeout=self.config.request_timeout * 4, max_mb=self.config.max_download_mb)
            return AssetInfo(
                scene_number=request.scene_number, media_type="video", local_path=str(dest), source="pixabay",
                source_id=hid, source_url=hit.get("pageURL", ""), author=hit.get("user", ""),
                author_url=f"https://pixabay.com/users/{hit.get('user', '')}-{hit.get('user_id', '')}/",
                license=LICENSE, downloaded_at=now_iso(), query=request.query,
                width=chosen.get("width", 0), height=chosen.get("height", 0), duration=float(hit.get("duration") or 0),
            )
        return None

    def _fetch_image(self, request: AssetRequest, dest_dir: Path) -> AssetInfo | None:
        data = self._get(
            "https://pixabay.com/api/",
            {"q": request.query[:100], "image_type": "photo", "orientation": "vertical",
             "per_page": max(3, self.config.per_page), "safesearch": "true"},
        )
        for hit in data.get("hits", []):
            hid = str(hit.get("id"))
            if hid in request.exclude_ids:
                continue
            url = hit.get("largeImageURL") or hit.get("webformatURL")
            if not url:
                continue
            dest = dest_dir / f"scene_{request.scene_number:02d}_pixabay_{hid}.jpg"
            download_file(url, dest, timeout=self.config.request_timeout * 2, max_mb=self.config.max_download_mb)
            return AssetInfo(
                scene_number=request.scene_number, media_type="image", local_path=str(dest), source="pixabay",
                source_id=hid, source_url=hit.get("pageURL", ""), author=hit.get("user", ""),
                license=LICENSE, downloaded_at=now_iso(), query=request.query,
                width=hit.get("imageWidth", 0), height=hit.get("imageHeight", 0),
            )
        return None
