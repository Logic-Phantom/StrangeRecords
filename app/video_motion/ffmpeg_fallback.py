"""AI 영상이 없는 장면: 기존 AI 정지 이미지에 FFmpeg 움직임 효과 (외부 API 없음, CPU 만 사용).

움직임 종류 (장면 설명·나레이션·중요도로 고르고, 같은 효과가 연속되지 않게 한다):
  slow_zoom_in / slow_zoom_out / pan_left / pan_right   느린 카메라 이동 (기존 Ken Burns 보다 약하게)
  parallax       가운데(전경)와 바깥(배경)이 반대로 아주 조금 움직이는 2층 이동 (부드러운 타원 마스크)
  fog_overlay    느리게 흐르는 안개 텍스처를 screen 합성
  light_flicker  촛불/등불처럼 천천히 변하는 밝기 (빠른 점멸 없음)
  dust_overlay   천천히 떠다니는 먼지 입자
  none           움직임 없음

- 원본 이미지는 읽기만 한다. 텍스처/마스크는 작업 폴더(work/motion)에 Pillow 로 한 번 만든다.
- 출력은 기존 Scene 클립과 같은 1080x1920 / 30fps / H.264 yuv420p / 무음.
- AI 영상이 장면보다 짧으면 max_slowdown 배율까지 느리게 재생하고 나머지는 마지막 프레임을 유지한다.
"""

from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from app.config.settings import VideoConfig
from app.schemas import Scene
from app.utils.logger import get_logger
from app.video.ffmpeg import media_duration, run_ffmpeg
from app.video.scenes import image_filter

logger = get_logger("motion_fallback")

MOTIONS = ("slow_zoom_in", "slow_zoom_out", "pan_left", "pan_right", "parallax", "fog_overlay", "light_flicker",
           "dust_overlay", "none")
SLOW = 0.65   # 기존 zoom_amount 대비 움직임 크기

# 장면 설명(영어 visual_prompt) / 나레이션(한국어)에 나오는 단어 → 어울리는 움직임
_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("fog_overlay", ("fog", "mist", "smoke", "haze", "swamp", "steam", "cloud", "안개", "연기", "늪", "수증기", "구름")),
    ("light_flicker", ("candle", "lamp", "lantern", "torch", "fire", "flame", "flicker", "lightning", "neon", "bulb",
                       "촛불", "등불", "횃불", "불빛", "번개", "전등", "불꽃", "깜빡")),
    ("dust_overlay", ("dust", "ancient", "ruin", "tomb", "archive", "document", "manuscript", "attic", "abandoned",
                      "old book", "relic", "먼지", "유적", "무덤", "고문서", "기록", "문서", "폐허", "고대", "유물")),
    ("parallax", ("forest", "corridor", "hallway", "tunnel", "street", "alley", "village", "crowd", "숲", "복도",
                  "터널", "골목", "마을", "거리")),
    ("pan", ("landscape", "horizon", "ocean", "sea", "desert", "mountain", "skyline", "aerial", "coast", "field",
             "바다", "사막", "산", "해안", "지평선", "들판", "하늘")),
    ("slow_zoom_in", ("face", "eye", "close-up", "closeup", "symbol", "clue", "evidence", "footprint", "letter",
                      "photo", "map", "단서", "증거", "발자국", "편지", "사진", "지도", "표식", "흔적")),
]
_LEGACY = {"zoom_in": "slow_zoom_in", "zoom_out": "slow_zoom_out", "pan_left": "pan_left", "pan_right": "pan_right",
           "pan_up": "parallax", "pan_down": "parallax", "static": "none"}
_ROTATION = ("slow_zoom_in", "pan_right", "slow_zoom_out", "pan_left", "parallax")


def choose_motion(scene: Scene, priority: int, previous: str | None, is_last: bool = False) -> str:
    """장면 내용으로 움직임을 고른다 (Gemini 추가 호출 없음). 직전 장면과 같은 효과는 피한다."""
    if scene.effect in _LEGACY and scene.effect != "auto":
        motion = _LEGACY[scene.effect]
        if motion != previous:
            return motion
    text = f"{scene.visual_prompt} {scene.visual} {scene.narration}".lower()
    candidates: list[str] = []
    for motion, words in _KEYWORDS:
        if any(w in text for w in words):
            if motion == "pan":
                candidates += ["pan_right", "pan_left"] if scene.scene_number % 2 else ["pan_left", "pan_right"]
            else:
                candidates.append(motion)
    if priority == 1:
        candidates.insert(0, "slow_zoom_in")      # 첫 장면 Hook: 천천히 다가가는 카메라
    if is_last:
        candidates.append("slow_zoom_out")       # 마지막 장면: 멀어지며 마무리
    rng = random.Random(f"{scene.scene_number}-{scene.visual_prompt}")
    rotation = list(_ROTATION)
    rng.shuffle(rotation)
    for motion in [*candidates, *rotation]:
        if motion != previous:
            return motion
    return "slow_zoom_in"


def plan_motions(scenes: list[Scene], priorities: dict[int, int]) -> dict[int, str]:
    out: dict[int, str] = {}
    previous: str | None = None
    for i, scene in enumerate(scenes):
        motion = choose_motion(scene, priorities.get(scene.scene_number, 4), previous, is_last=i == len(scenes) - 1)
        out[scene.scene_number] = motion
        previous = motion
    return out


# ------------------------------------------------------------------ textures (Pillow, 한 번만 생성)
def _fog_texture(path: Path, w: int, h: int, seed: str) -> Path:
    if path.exists():
        return path
    rng = random.Random(seed)
    small = Image.new("L", (max(w // 12, 8), max(h // 12, 8)))
    small.putdata([rng.randint(0, 255) for _ in range(small.width * small.height)])
    fog = small.filter(ImageFilter.GaussianBlur(3)).resize((w, h), Image.BICUBIC).filter(ImageFilter.GaussianBlur(18))
    # 대비를 키워 구름 덩어리처럼, 전체적으로는 어둡게 (screen 합성 시 은은하게 밝아짐)
    fog = fog.point(lambda v: max(0, min(255, int((v - 110) * 2.2))))
    path.parent.mkdir(parents=True, exist_ok=True)
    fog.convert("RGB").save(path)
    return path


def _dust_texture(path: Path, w: int, h: int, seed: str) -> Path:
    if path.exists():
        return path
    rng = random.Random(seed)
    img = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(img)
    for _ in range(int(w * h / 9000)):
        x, y, r = rng.uniform(0, w), rng.uniform(0, h), rng.choice((1, 1, 1.5, 2, 2.5))
        draw.ellipse([x - r, y - r, x + r, y + r], fill=rng.randint(90, 230))
    img = img.filter(ImageFilter.GaussianBlur(0.8))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(path)
    return path


def _ellipse_mask(path: Path, w: int, h: int) -> Path:
    if path.exists():
        return path
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).ellipse([w * 0.18, h * 0.2, w * 0.82, h * 0.8], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(min(w, h) * 0.09))
    path.parent.mkdir(parents=True, exist_ok=True)
    mask.save(path)
    return path


def _slow_cfg(cfg: VideoConfig) -> VideoConfig:
    return cfg.model_copy(update={"zoom_amount": round(cfg.zoom_amount * SLOW, 4)})


def _even(v: float) -> int:
    n = int(round(v))
    return n + (n % 2)


def _encode(fps: int) -> list[str]:
    return ["-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", "-r", str(fps)]


# ------------------------------------------------------------------ builders
def build_motion_clip(image: str | Path, duration: float, motion: str, out: Path, cfg: VideoConfig,
                      extra_filter: str = "", texture_dir: Path | None = None, seed: str = "") -> Path:
    """정지 이미지 → 움직임 클립. extra_filter 는 첫 장면 Hook 효과(1080x1920 결과 위에 덧붙임)."""
    w, h, fps = cfg.width, cfg.height, cfg.fps
    duration = max(duration, 0.5)
    d = f"{duration:.3f}"
    dl = f"{duration + 0.25:.3f}"   # 입력은 조금 길게 → 출력이 프레임 하나 모자라지 않게 (xfade 길이 유지)
    extra = f",{extra_filter}" if extra_filter else ""
    textures = texture_dir or out.parent / "motion_textures"
    slow = _slow_cfg(cfg)
    motion = motion if motion in MOTIONS else "slow_zoom_in"
    image = str(image)

    if motion in ("slow_zoom_in", "slow_zoom_out", "pan_left", "pan_right"):
        effect = {"slow_zoom_in": "zoom_in", "slow_zoom_out": "zoom_out"}.get(motion, motion)
        vf = image_filter(effect, duration, slow) + f"{extra},format=yuv420p"
        run_ffmpeg(["-i", image, "-t", d, "-vf", vf, *_encode(fps), str(out)], step=f"motion {motion} {out.name}")
        return out

    if motion == "none":
        vf = image_filter("static", duration, cfg) + f"{extra},format=yuv420p"
        run_ffmpeg(["-loop", "1", "-framerate", str(fps), "-i", image, "-t", d, "-vf", vf, *_encode(fps), str(out)],
                   step=f"motion none {out.name}")
        return out

    if motion == "light_flicker":
        # 0.5~2.3Hz 의 느린 밝기 변화 두 개를 겹침 (빠른 점멸 없음, 진폭 ±5% 이하)
        flicker = "eq=brightness='0.035*sin(2*PI*t*0.55)+0.018*sin(2*PI*t*2.3+1.3)':eval=frame"
        vf = image_filter("zoom_in", duration, slow) + f",format=yuv420p,{flicker}{extra},format=yuv420p"
        run_ffmpeg(["-i", image, "-t", d, "-vf", vf, *_encode(fps), str(out)], step=f"motion flicker {out.name}")
        return out

    if motion in ("fog_overlay", "dust_overlay"):
        if motion == "fog_overlay":
            tex = _fog_texture(textures / f"fog_{w}x{h}.png", _even(w * 1.5), h, seed or "fog")
            base = image_filter("zoom_in", duration, slow)
            # 안개는 왼쪽 → 오른쪽으로 천천히 흐름
            move = f"crop={w}:{h}:x='(iw-ow)*t/{d}':y=0"
            blend = "blend=all_mode=screen:all_opacity=0.28"
        else:
            tex = _dust_texture(textures / f"dust_{w}x{h}.png", _even(w * 1.1), _even(h * 1.35), seed or "dust")
            base = image_filter("zoom_out", duration, slow)
            # 먼지는 위에서 아래로, 옆으로 살짝 흘러감
            move = f"crop={w}:{h}:x='(iw-ow)*(0.5+0.4*sin(t/{d}*3.14))':y='(ih-oh)*(1-t/{d})'"
            blend = "blend=all_mode=screen:all_opacity=0.45"
        graph = (f"[0:v]{base},format=gbrp[b];"
                 f"[1:v]{move},setsar=1,fps={fps},format=gbrp[o];"
                 f"[b][o]{blend},format=yuv420p{extra},format=yuv420p[v]")
        run_ffmpeg(["-i", image, "-loop", "1", "-framerate", str(fps), "-t", dl, "-i", str(tex),
                    "-filter_complex", graph, "-map", "[v]", "-t", d, *_encode(fps), str(out)],
                   step=f"motion {motion} {out.name}")
        return out

    # parallax: 같은 배율의 두 층을 반대 방향으로 아주 조금 이동 (가운데 시점에서 두 층이 일치 → 경계가 거의 보이지 않음)
    mask = _ellipse_mask(textures / f"mask_{w}x{h}.png", w, h)
    W2, H2 = _even(w * 1.1), _even(h * 1.1)
    cover = f"scale={W2}:{H2}:force_original_aspect_ratio=increase:flags=lanczos,crop={W2}:{H2},setsar=1"
    graph = (f"[0:v]{cover},split[a][c];"
             f"[a]crop={w}:{h}:x='(iw-ow)*(0.62-0.24*t/{d})':y='(ih-oh)*(0.55-0.1*t/{d})',gblur=sigma=1.2[bg];"
             f"[c]crop={w}:{h}:x='(iw-ow)*(0.38+0.24*t/{d})':y='(ih-oh)*(0.45+0.1*t/{d})',format=rgba[fgc];"
             f"[1:v]format=gray,scale={w}:{h}[m];[fgc][m]alphamerge[fg];"
             f"[bg][fg]overlay=0:0:format=auto,fps={fps},format=yuv420p{extra},format=yuv420p[v]")
    run_ffmpeg(["-loop", "1", "-framerate", str(fps), "-t", dl, "-i", image,
                "-loop", "1", "-framerate", str(fps), "-t", dl, "-i", str(mask),
                "-filter_complex", graph, "-map", "[v]", "-t", d, *_encode(fps), str(out)],
               step=f"motion parallax {out.name}")
    return out


def build_ai_clip(video: str | Path, duration: float, out: Path, cfg: VideoConfig, max_slowdown: float = 1.6,
                  extra_filter: str = "") -> dict:
    """AI 영상을 장면 길이에 맞춘다: 9:16 cover crop → (짧으면) 느리게 + 마지막 프레임 유지 → 30fps 무음."""
    w, h, fps = cfg.width, cfg.height, cfg.fps
    duration = max(duration, 0.5)
    src = media_duration(video)
    if src <= 0:
        raise ValueError(f"AI 영상 길이 확인 실패: {video}")
    factor = 1.0
    if src < duration:
        factor = min(duration / src, max(max_slowdown, 1.0))
    hold = max(duration - src * factor, 0.0)
    vf = (f"setpts={factor:.4f}*(PTS-STARTPTS),"
          f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h},setsar=1,fps={fps}")
    if hold > 0.01:
        vf += f",tpad=stop_mode=clone:stop_duration={hold + 0.2:.3f}"
    if extra_filter:
        vf += f",{extra_filter}"
    vf += ",format=yuv420p"
    run_ffmpeg(["-i", str(video), "-t", f"{duration:.3f}", "-vf", vf, *_encode(fps), str(out)],
               step=f"ai clip {out.name}")
    return {"source_seconds": round(src, 3), "slowdown": round(factor, 3), "hold_seconds": round(hold, 3)}
