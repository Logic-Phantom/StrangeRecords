"""프로젝트 로컬 자료 (assets/images, assets/videos).

파일명 또는 같은 이름의 .json 사이드카(tags, author, license, source_url)로 검색어와 매칭한다.
"""

from __future__ import annotations

import json
import random
import re
import shutil
from pathlib import Path

from app.assets.base import AssetProvider, AssetRequest
from app.schemas import AssetInfo
from app.utils.files import now_iso

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm"}


def _words(text: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9가-힣]+", text.lower()) if len(w) >= 2}


class LocalAssetProvider(AssetProvider):
    name = "local"

    def __init__(self, image_dir: Path, video_dir: Path, require_match: bool = True):
        self.image_dir = image_dir
        self.video_dir = video_dir
        self.require_match = require_match

    def _files(self, media_type: str) -> list[Path]:
        folder, exts = (self.video_dir, VIDEO_EXT) if media_type == "video" else (self.image_dir, IMAGE_EXT)
        if not folder.exists():
            return []
        return [p for p in folder.rglob("*") if p.suffix.lower() in exts]

    @staticmethod
    def _meta(path: Path) -> dict:
        sidecar = path.with_suffix(".json")
        if sidecar.exists():
            try:
                return json.loads(sidecar.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
        return {}

    def fetch(self, request: AssetRequest, dest_dir: Path) -> AssetInfo | None:
        query_words = _words(request.query)
        best: tuple[int, Path, dict] | None = None
        candidates: list[tuple[int, Path, dict]] = []
        for media_type in ([request.media_type] + [t for t in ("video", "image") if t != request.media_type]):
            for path in self._files(media_type):
                key = f"local:{path.name}"
                if key in request.exclude_ids:
                    continue
                meta = self._meta(path)
                words = _words(path.stem) | _words(" ".join(meta.get("tags", [])))
                candidates.append((len(words & query_words), path, meta))
        if not candidates:
            return None
        matched = [c for c in candidates if c[0] > 0]
        if matched:
            best = max(matched, key=lambda c: c[0])
        elif not self.require_match:
            best = random.choice(candidates)
        if not best:
            return None

        _, path, meta = best
        media_type = "video" if path.suffix.lower() in VIDEO_EXT else "image"
        dest = dest_dir / f"scene_{request.scene_number:02d}_local_{path.name}"
        shutil.copy2(path, dest)
        return AssetInfo(
            scene_number=request.scene_number, media_type=media_type, local_path=str(dest), source="local",
            source_id=f"local:{path.name}", source_url=meta.get("source_url", ""), author=meta.get("author", ""),
            license=meta.get("license", "채널 보유 자료"), downloaded_at=now_iso(), query=request.query,
        )
