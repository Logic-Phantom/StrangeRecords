"""BGM / 효과음 라이브러리 (assets/music, assets/sfx).

assets/music/music.json 에 각 곡의 title, artist, source, license, url, mood 를 기록한다.
저작권 문제가 없는 음원만 넣는다.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from app.schemas import AudioTrackInfo, SFX_NAMES

AUDIO_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}


class AudioLibrary:
    def __init__(self, music_dir: Path, sfx_dir: Path):
        self.music_dir = music_dir
        self.sfx_dir = sfx_dir

    def _music_meta(self) -> dict[str, dict]:
        meta_file = self.music_dir / "music.json"
        if not meta_file.exists():
            return {}
        items = json.loads(meta_file.read_text(encoding="utf-8"))
        return {item["file"]: item for item in items if "file" in item}

    def tracks(self) -> list[AudioTrackInfo]:
        meta = self._music_meta()
        tracks = []
        for path in sorted(self.music_dir.glob("*")):
            if path.suffix.lower() not in AUDIO_EXT:
                continue
            info = meta.get(path.name, {})
            # 메타데이터(라이선스)가 없는 음원은 저작권 확인 불가 → 사용하지 않음
            if not info.get("license"):
                continue
            tracks.append(AudioTrackInfo(**{**info, "file": str(path)}))
        return tracks

    def pick_bgm(self, mood: str, seed: str | None = None) -> AudioTrackInfo | None:
        tracks = self.tracks()
        if not tracks:
            return None
        matching = [t for t in tracks if t.mood == mood] or tracks
        return random.Random(seed).choice(matching)

    def sfx(self, name: str) -> Path | None:
        if name not in SFX_NAMES:
            return None
        for ext in (".wav", ".mp3", ".ogg", ".m4a"):
            path = self.sfx_dir / f"{name}{ext}"
            if path.exists():
                return path
        return None
