"""Scene 하나를 1080x1920 30fps 무음 클립으로 만든다.

- 영상: 9:16 으로 Crop(cover) + 길이가 짧으면 반복
- 이미지: Ken Burns (zoom in/out, pan) 로 움직임 부여
"""

from __future__ import annotations

import random
from pathlib import Path

from app.config.settings import VideoConfig
from app.schemas import AssetInfo
from app.video.ffmpeg import media_duration, run_ffmpeg

IMAGE_EFFECTS = ("zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "pan_down")


def _cover(w: int, h: int) -> str:
    return f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h}"


def image_filter(effect: str, duration: float, cfg: VideoConfig, seed: str = "") -> str:
    w, h, fps = cfg.width, cfg.height, cfg.fps
    frames = max(int(round(duration * fps)), 1)
    z = cfg.zoom_amount
    if effect in ("auto", "", None):
        effect = random.Random(seed).choice(IMAGE_EFFECTS)
    # 확대한 캔버스 위에서 zoompan → 떨림 감소
    pre = f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase:flags=lanczos,crop={w * 2}:{h * 2},setsar=1"
    center_x = "iw/2-(iw/zoom/2)"
    center_y = "ih/2-(ih/zoom/2)"
    if effect == "zoom_in":
        zexpr, x, y = f"1+{z}*on/{frames}", center_x, center_y
    elif effect == "zoom_out":
        zexpr, x, y = f"{1 + z}-{z}*on/{frames}", center_x, center_y
    elif effect == "pan_left":
        zexpr, x, y = f"{1 + z}", f"(iw-iw/zoom)*(1-on/{frames})", center_y
    elif effect == "pan_right":
        zexpr, x, y = f"{1 + z}", f"(iw-iw/zoom)*on/{frames}", center_y
    elif effect == "pan_up":
        zexpr, x, y = f"{1 + z}", center_x, f"(ih-ih/zoom)*(1-on/{frames})"
    elif effect == "pan_down":
        zexpr, x, y = f"{1 + z}", center_x, f"(ih-ih/zoom)*on/{frames}"
    else:  # static
        return f"{_cover(w, h)},setsar=1,fps={fps}"
    return f"{pre},zoompan=z='{zexpr}':x='{x}':y='{y}':d={frames}:s={w}x{h}:fps={fps},setsar=1"


def build_scene_clip(asset: AssetInfo, duration: float, effect: str, out: Path, cfg: VideoConfig) -> Path:
    w, h, fps = cfg.width, cfg.height, cfg.fps
    duration = max(duration, 0.5)
    encode = ["-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", "-r", str(fps)]

    if asset.media_type == "video":
        src_duration = asset.duration or media_duration(asset.local_path)
        # 앞부분(인트로/로고)을 살짝 건너뛰되 장면 길이는 확보
        offset = min(1.0, max(0.0, (src_duration - duration) / 3)) if src_duration > duration else 0.0
        vf = f"{_cover(w, h)},setsar=1,fps={fps},format=yuv420p"
        loop = ["-stream_loop", "-1"] if src_duration < duration + offset else []
        run_ffmpeg(
            [*loop, "-ss", f"{offset:.2f}", "-i", asset.local_path, "-t", f"{duration:.3f}", "-vf", vf, *encode, str(out)],
            step=f"scene clip {out.name}",
        )
    else:
        if effect in ("auto", "", None):
            effect = random.Random(asset.local_path).choice(IMAGE_EFFECTS)
        vf = image_filter(effect, duration, cfg) + ",format=yuv420p"
        # zoompan 은 한 장에서 d 프레임을 생성, static 은 이미지를 반복 입력
        loop = ["-loop", "1", "-framerate", str(fps)] if effect == "static" else []
        run_ffmpeg(
            [*loop, "-i", asset.local_path, "-t", f"{duration:.3f}", "-vf", vf, *encode, str(out)],
            step=f"scene clip {out.name}",
        )
    return out
