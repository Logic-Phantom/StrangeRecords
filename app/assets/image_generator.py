"""이미지 생성 Provider.

- GeminiImageProvider: Scene 의 visual_prompt(대본 기반)로 Gemini 이미지 모델이 장면 이미지를 생성
  (config.assets.ai_image.enabled=true 일 때만, generated_by_ai=true 기록)
- ProceduralImageProvider: 외부 서비스 없이 Pillow 로 분위기 배경을 그리는 최종 fallback (저작권 문제 없음)
"""

from __future__ import annotations

import hashlib
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from app.assets.base import AssetProvider, AssetRequest
from app.schemas import AssetInfo
from app.utils.files import now_iso
from app.utils.logger import get_logger

logger = get_logger("image_gen")

MOOD_PALETTES = {
    "dark": [((8, 10, 22), (40, 18, 48)), ((5, 18, 24), (20, 60, 70)), ((15, 8, 8), (70, 20, 20))],
    "mystery": [((6, 14, 30), (30, 70, 110)), ((10, 25, 20), (40, 90, 70)), ((20, 12, 35), (90, 60, 140))],
    "light": [((20, 30, 70), (240, 120, 90)), ((10, 60, 90), (250, 200, 90)), ((40, 20, 80), (90, 200, 220))],
}


MOOD_STYLES = {
    "dark": "dark, eerie, moody low-key lighting, desaturated cold tones, light fog",
    "mystery": "mysterious, atmospheric volumetric light, deep blue and teal tones",
    "light": "bright, playful, vivid colors, soft daylight",
}


def build_image_prompt(request: AssetRequest, style: str = "") -> str:
    """Scene 의 visual_prompt + 영상 전체 이야기 + 공통 스타일 → 장면마다 같은 이야기를 그리는 프롬프트."""
    parts = [request.visual_prompt or request.query]
    if request.context:
        parts.append(f"This is one scene of a short documentary-style story about: {request.context}")
    parts.append(f"Style: {style or 'cinematic photorealistic still frame'}, {MOOD_STYLES.get(request.mood, MOOD_STYLES['dark'])}")
    parts.append(
        "Vertical 9:16 portrait composition with the main subject in the center, high detail."
        " No text, no letters, no captions, no watermark, no logos, no recognizable real person faces."
    )
    return ". ".join(p.strip().rstrip(".") for p in parts if p and p.strip()) + "."


class GeminiImageProvider(AssetProvider):
    name = "ai_image"
    supports = ("image",)

    def __init__(self, client, models: str | list[str], enabled: bool, style: str = ""):
        self.client = client
        self.models = [m for m in ([models] if isinstance(models, str) else models) if m]
        self.enabled = enabled
        self.style = style
        self._tried: set[int] = set()

    @property
    def available(self) -> bool:
        return bool(self.enabled and self.client and self.models)

    def fetch(self, request: AssetRequest, dest_dir: Path) -> AssetInfo | None:
        # 프롬프트는 검색어와 무관(visual_prompt)하므로 한 Scene 에 한 번만 시도한다 (실패 시 다음 provider 로)
        if request.scene_number in self._tried:
            return None
        self._tried.add(request.scene_number)
        prompt = build_image_prompt(request, self.style)
        data, mime, model = self.client.generate_image(prompt, self.models, aspect_ratio="9:16")
        ext = ".jpg" if "jpeg" in (mime or "") else ".png"
        dest = dest_dir / f"scene_{request.scene_number:02d}_ai{ext}"
        dest.write_bytes(data)
        with Image.open(dest) as img:
            w, h = img.size
        return AssetInfo(
            scene_number=request.scene_number, media_type="image", local_path=str(dest), source="ai_image",
            source_id=hashlib.md5(prompt.encode()).hexdigest()[:12], license=f"AI generated (Gemini {model})",
            downloaded_at=now_iso(), generated_by_ai=True, query=prompt, width=w, height=h,
        )


def render_procedural(path: Path, seed: str, mood: str = "dark", size: tuple[int, int] = (1080, 1920)) -> Path:
    rng = random.Random(seed)
    w, h = size
    top, bottom = rng.choice(MOOD_PALETTES.get(mood, MOOD_PALETTES["dark"]))
    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / (h - 1)
        draw.line([(0, y), (w, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))

    # 흐릿한 빛 번짐 (안개/심해 느낌)
    glow = Image.new("RGBA", size, (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    for _ in range(rng.randint(4, 8)):
        cx, cy = rng.randint(0, w), rng.randint(int(h * 0.1), int(h * 0.9))
        r = rng.randint(150, 520)
        color = tuple(min(255, c + rng.randint(30, 90)) for c in bottom) + (rng.randint(35, 80),)
        gdraw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    glow = glow.filter(ImageFilter.GaussianBlur(120))
    img = Image.alpha_composite(img.convert("RGBA"), glow)

    # 비네팅
    vignette = Image.new("L", size, 0)
    vdraw = ImageDraw.Draw(vignette)
    steps = 40
    for i in range(steps):
        alpha = int(170 * (1 - i / steps) ** 2)
        inset = int(i * min(w, h) / (steps * 2.2))
        vdraw.rectangle([inset, inset, w - inset, h - inset], outline=alpha, width=int(min(w, h) / (steps * 2.2)) + 1)
    vignette = vignette.filter(ImageFilter.GaussianBlur(60))
    black = Image.new("RGBA", size, (0, 0, 0, 255))
    img = Image.composite(black, img, vignette)

    # 필름 그레인
    noise = Image.effect_noise(size, 18).convert("RGBA")
    noise.putalpha(22)
    img = Image.alpha_composite(img, noise)

    path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(path, quality=92)
    return path


class ProceduralImageProvider(AssetProvider):
    name = "procedural"
    supports = ("image", "video")

    def fetch(self, request: AssetRequest, dest_dir: Path) -> AssetInfo | None:
        dest = dest_dir / f"scene_{request.scene_number:02d}_procedural.jpg"
        render_procedural(dest, seed=f"{request.query}-{request.scene_number}", mood=request.mood)
        return AssetInfo(
            scene_number=request.scene_number, media_type="image", local_path=str(dest), source="procedural",
            source_id=f"procedural-{request.scene_number}", license="자체 제작 (저작권 문제 없음)",
            downloaded_at=now_iso(), query=request.query, width=1080, height=1920,
        )
